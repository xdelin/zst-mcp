"""Hard 桶：shell 层本地补丁治理单测（配额 / job 上限 / 完成即回收，假件确定性）。

运行（.7 宿主机，或直接跑 docker/gateway/run_regression.sh）：
  docker cp docker/patches/test_shell_layer_hard.py docker-agent_backend-1:/tmp/
  docker exec docker-agent_backend-1 python -m pytest /tmp/test_shell_layer_hard.py -q

导入的是 agent_backend 容器内挂载补丁 dify_agent.layers.shell.layer（
= 仓库 docker/patches/dify_agent_shell_layer.py）。全部用 duck-type 假件
（假 RuntimeLease / 假 ShellCommandProtocol），不碰真沙箱、不碰真 docker、
不跑真 du —— 配额脚本的实际执行留给 easy/black-box 桶对真沙箱验证。
覆盖：
  1. 磁盘配额：on_context_create / on_context_resume 前置检查，脚本结构
     （set -eu、test -d、du -sm、2048 阈值），exit 0 放行、非 0 抛
     "Binding disk quota exceeded"；内部 job 用完即 delete 清理。
  2. 单轮 job 上限：32 个未完成 job 时 shell_run 返回错误 observation
     （不启动新 job，无泄漏）；上限只统计未完成——完成即回收后再跑能通过。
  3. 完成即回收：done=true 的 job 在 run/wait 交付 observation 后立即移出
     台账；未完成 job 保持跟踪、wait 到 done 后回收。
  4. 台账不变量：job_ids 去重、job_offsets ⊆ job_ids（模型层校验兜底）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dify_agent.layers.shell.configs import DifyShellLayerConfig
from dify_agent.layers.shell.layer import DifyShellLayer, DifyShellRuntimeState
from dify_agent.adapters.shell.protocols import ShellCommandResult
from dify_agent.runtime_backend import RuntimeLayout


class FakeCommands:
    """按脚本回报预制 ShellCommandResult 的假 shellctl。"""

    def __init__(self, run_results: list[ShellCommandResult] | None = None):
        self.run_results = list(run_results or [])
        self.run_scripts: list[str] = []
        self.wait_calls: list[tuple[str, int]] = []
        self.deleted: list[str] = []

    def _next_run_result(self) -> ShellCommandResult:
        if len(self.run_results) > 1:
            return self.run_results.pop(0)
        if not self.run_results:
            raise AssertionError("unexpected commands.run() call")
        return self.run_results[0]

    async def run(self, script, cwd=None, env=None, timeout=None, mode=None, **kw):
        self.run_scripts.append(script)
        return self._next_run_result()

    async def wait(self, job_id, offset=0, timeout=None, **kw):
        self.wait_calls.append((job_id, offset))
        return self._next_run_result()

    async def read_output(self, job_id, offset=0):
        return self._next_run_result()

    async def input(self, job_id, text, offset=0, timeout=None):
        return self._next_run_result()

    async def interrupt(self, job_id, grace_seconds=None):
        return self._next_run_result()

    async def tail(self, job_id):
        return self._next_run_result()

    async def delete(self, job_id, force=False):
        self.deleted.append(job_id)


def make_result(job_id="j1", *, done=True, exit_code=0, output="", truncated=False, offset=None) -> ShellCommandResult:
    return ShellCommandResult(
        job_id=job_id,
        status="exited" if done else "running",
        done=done,
        exit_code=exit_code,
        output=output,
        offset=len(output) if offset is None else offset,
        truncated=truncated,
    )


def make_layer(commands: FakeCommands) -> DifyShellLayer:
    lease = SimpleNamespace(
        layout=RuntimeLayout(home_dir="/home/dify", workspace_dir="/home/dify/workspace"),
        commands=commands,
    )
    layer = DifyShellLayer(config=DifyShellLayerConfig.model_validate({}))
    layer.deps = SimpleNamespace(runtime=SimpleNamespace(lease=lease), execution_context=None)
    return layer


# ---- 1. 磁盘配额


def test_quota_script_shape_and_pass():
    commands = FakeCommands([make_result("q1", done=True, exit_code=0, output="12\n")])
    layer = make_layer(commands)
    asyncio.run(layer.on_context_create())  # 放行不抛
    assert len(commands.run_scripts) == 1
    script = commands.run_scripts[0]
    assert script.startswith("set -eu")
    assert "test -d /home/dify\n" in script
    assert "test -d /home/dify/workspace\n" in script
    assert "du -sm /home/dify /home/dify/workspace" in script
    assert "-gt 2048" in script
    # 内部检查 job 用完即清理，不留句柄
    assert commands.deleted == ["q1"]


def test_quota_checked_on_resume_too():
    commands = FakeCommands([make_result("q2", done=True, exit_code=0, output="3\n")])
    layer = make_layer(commands)
    asyncio.run(layer.on_context_resume())
    assert len(commands.run_scripts) == 1


def test_quota_exceeded_raises_runtime_error():
    detail = "disk quota exceeded: 3000MB used > 2048MB limit (home + workspace)"
    commands = FakeCommands([make_result("q3", done=True, exit_code=1, output=detail + "\n")])
    layer = make_layer(commands)
    with pytest.raises(RuntimeError, match="Binding disk quota exceeded"):
        asyncio.run(layer.on_context_create())


# ---- 2. 单轮 job 上限


def _fill_unfinished(layer: DifyShellLayer, n: int) -> None:
    state = layer.runtime_state
    ids = [f"j{i}" for i in range(n)]
    state.job_ids = ids
    state.job_offsets = {}


def test_job_limit_blocks_run_without_starting():
    commands = FakeCommands()
    layer = make_layer(commands)
    _fill_unfinished(layer, 32)
    result = asyncio.run(layer._tool_run("echo hi"))
    assert isinstance(result, dict) and "error" in result
    assert "shell job limit" in result["error"]
    assert "32" in result["error"]
    assert commands.run_scripts == []  # 不启动 job，无泄漏


def test_limit_counts_only_unfinished_jobs():
    commands = FakeCommands()
    layer = make_layer(commands)
    _fill_unfinished(layer, 32)
    blocked = asyncio.run(layer._tool_run("echo a"))
    assert isinstance(blocked, dict) and "error" in blocked

    # 回收一个（wait 到 done）后腾出名额：新 job 能启动，台账回到 32
    commands.run_results = [make_result("j0", done=True, exit_code=0, output="ok")]
    waited = asyncio.run(layer._tool_wait("j0"))
    assert isinstance(waited, str) and '"done":true' in waited
    assert "j0" not in layer.runtime_state.job_ids

    commands.run_results = [make_result("jnew", done=False, exit_code=None, output="running...")]
    ok = asyncio.run(layer._tool_run("sleep 30"))
    assert isinstance(ok, str) and '"job_id":"jnew"' in ok
    assert len(layer.runtime_state.job_ids) == 32

    # 新的未完成 job 占满后再次拦截
    blocked_again = asyncio.run(layer._tool_run("echo b"))
    assert isinstance(blocked_again, dict) and "error" in blocked_again


# ---- 3. 完成即回收


def test_finished_job_reclaimed_on_run():
    commands = FakeCommands([make_result("jd1", done=True, exit_code=0, output="hello")])
    layer = make_layer(commands)
    obs = asyncio.run(layer._tool_run("echo hello"))
    assert isinstance(obs, str)
    assert '"job_id":"jd1"' in obs and '"done":true' in obs
    assert layer.runtime_state.job_ids == []
    assert layer.runtime_state.job_offsets == {}


def test_unfinished_job_tracked_then_reclaimed_on_wait():
    commands = FakeCommands(
        [
            make_result("jr1", done=False, exit_code=None, output="partial"),
            make_result("jr1", done=True, exit_code=0, output="partial + final"),
        ]
    )
    layer = make_layer(commands)
    obs1 = asyncio.run(layer._tool_run("long-job"))
    assert isinstance(obs1, str)
    assert layer.runtime_state.job_ids == ["jr1"]  # 未完成保持跟踪

    obs2 = asyncio.run(layer._tool_wait("jr1"))
    assert isinstance(obs2, str) and '"done":true' in obs2
    assert layer.runtime_state.job_ids == []  # wait 交付后立即回收
    assert layer.runtime_state.job_offsets == {}


def test_run_wait_delivers_metadata_tag():
    commands = FakeCommands([make_result("jm1", done=True, exit_code=3, output="boom")])
    layer = make_layer(commands)
    obs = asyncio.run(layer._tool_run("false"))
    assert isinstance(obs, str)
    assert obs.startswith("<metadata>")
    head = obs.split("</metadata>")[0]
    meta = json.loads(head.removeprefix("<metadata>"))
    assert meta["job_id"] == "jm1"
    assert meta["done"] is True
    assert meta["exit_code"] == 3


# ---- 4. 台账不变量（模型层兜底校验）


def test_runtime_state_rejects_duplicate_job_ids():
    with pytest.raises(ValidationError):
        DifyShellRuntimeState(job_ids=["a", "a"], job_offsets={})


def test_runtime_state_rejects_unknown_offsets():
    with pytest.raises(ValidationError):
        DifyShellRuntimeState(job_ids=["a"], job_offsets={"ghost": 5})


def test_reclaim_keeps_other_jobs():
    commands = FakeCommands(
        [
            make_result("keep", done=False, exit_code=None, output="..."),
            make_result("gone", done=True, exit_code=0, output="done"),
        ]
    )
    layer = make_layer(commands)
    asyncio.run(layer._tool_run("sleep 30"))
    asyncio.run(layer._tool_run("echo done"))
    assert layer.runtime_state.job_ids == ["keep"]  # 只回收完成的那个
