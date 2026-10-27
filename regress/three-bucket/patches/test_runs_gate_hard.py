"""Hard 桶：create_run 并发闸门单测（runs 路由本地补丁，假件、确定性）。

运行（.7 宿主机，或直接跑 docker/gateway/run_regression.sh）：
  docker cp docker/patches/test_runs_gate_hard.py docker-agent_backend-1:/tmp/
  docker exec docker-agent_backend-1 python -m pytest /tmp/test_runs_gate_hard.py -q

导入的是 agent_backend 容器内挂载补丁 dify_agent.server.routes.runs（
= 仓库 docker/patches/dify_agent_runs_route.py）。闸门：全局并发 64、
每用户并发 2，超限 429；计数在 run task 结束的 done_callback 释放
（释放幂等：重复回调安全），调度器异常路径同步回滚。
不碰真 Redis、不碰真调度器、不发真 run。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dify_agent.layers.execution_context.configs import DIFY_EXECUTION_CONTEXT_LAYER_TYPE_ID
from dify_agent.protocol.schemas import CreateRunRequest, RunComposition, RunLayerSpec
from dify_agent.runtime.run_scheduler import SchedulerStoppingError

import dify_agent.server.routes.runs as runs_mod


@pytest.fixture(autouse=True)
def _clean_gate_state():
    runs_mod._user_active_runs.clear()
    yield
    runs_mod._user_active_runs.clear()


def _endpoint():
    router = runs_mod.create_runs_router(lambda: None, lambda: None)
    for route in router.routes:
        methods = getattr(route, "methods", None) or set()
        if "POST" in methods and route.path == "/runs":
            return route.endpoint
    raise AssertionError("create_run endpoint not found on router")


def _request(tenant: str = "t1", user: str = "u1", with_context: bool = True) -> CreateRunRequest:
    layers = []
    if with_context:
        layers.append(
            RunLayerSpec(
                name="ctx",
                type=DIFY_EXECUTION_CONTEXT_LAYER_TYPE_ID,
                config={"tenant_id": tenant, "user_id": user},
            )
        )
    return CreateRunRequest(composition=RunComposition(layers=layers))


class FakeScheduler:
    def __init__(self, active: int = 0, *, raise_on_create: Exception | None = None):
        self.active_tasks = {f"task-{i}": None for i in range(active)}
        self.raise_on_create = raise_on_create
        self.captured_callbacks: list = []

    async def create_run(self, request: CreateRunRequest):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return SimpleNamespace(run_id="run-1", status="running")


class FakeTask:
    def __init__(self, scheduler: FakeScheduler):
        self._scheduler = scheduler

    def add_done_callback(self, cb) -> None:
        self._scheduler.captured_callbacks.append(cb)


def _scheduler_with_task() -> FakeScheduler:
    # create_run 成功后从 active_tasks 取 task 挂 done_callback；预置一个同
    # run_id 的假 task 模拟"run 已被调度、尚未结束"。
    class S(FakeScheduler):
        def __init__(self):
            super().__init__()
            self.task = FakeTask(self)

        async def create_run(self, request):
            record = await super().create_run(request)
            self.active_tasks[record.run_id] = self.task
            return record

    return S()


def test_per_user_gate_blocks_third_run():
    runs_mod._user_active_runs["t1:u1"] = runs_mod._MAX_RUNS_PER_USER
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint()(request=_request(), scheduler=FakeScheduler()))
    assert exc.value.status_code == 429
    assert "per-user" in exc.value.detail
    # 被闸门拒绝时不得污染计数
    assert runs_mod._user_active_runs["t1:u1"] == runs_mod._MAX_RUNS_PER_USER


def test_global_gate_blocks_when_scheduler_full():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint()(request=_request(), scheduler=FakeScheduler(active=runs_mod._MAX_RUNS_GLOBAL)))
    assert exc.value.status_code == 429
    assert "global concurrent run limit" in exc.value.detail


def test_success_increments_and_release_is_idempotent():
    scheduler = _scheduler_with_task()
    resp = asyncio.run(_endpoint()(request=_request(), scheduler=scheduler))
    assert resp.run_id == "run-1"
    assert runs_mod._user_active_runs == {"t1:u1": 1}
    cb = scheduler.captured_callbacks[0]
    cb(None)
    assert "t1:u1" not in runs_mod._user_active_runs
    cb(None)  # 释放幂等：done_callback 被重复触发不报错、不出现负数
    assert "t1:u1" not in runs_mod._user_active_runs


def test_two_users_count_independently():
    scheduler = _scheduler_with_task()
    asyncio.run(_endpoint()(request=_request(tenant="t1", user="u1"), scheduler=scheduler))
    asyncio.run(_endpoint()(request=_request(tenant="t1", user="u2"), scheduler=scheduler))
    assert runs_mod._user_active_runs == {"t1:u1": 1, "t1:u2": 1}


def test_fast_completion_releases_immediately():
    # task 未出现在 active_tasks（同步跑完）：立即释放而不是泄漏计数
    resp = asyncio.run(_endpoint()(request=_request(), scheduler=FakeScheduler()))
    assert resp.run_id == "run-1"
    assert runs_mod._user_active_runs == {}


def test_scheduler_error_rolls_back_count():
    scheduler = FakeScheduler(raise_on_create=SchedulerStoppingError("stopping"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint()(request=_request(), scheduler=scheduler))
    assert exc.value.status_code == 503
    assert runs_mod._user_active_runs == {}


def test_missing_context_falls_back_to_shared_key():
    # 组合里没有 execution_context 层时落到 unknown-tenant:no-user，闸门仍然生效
    runs_mod._user_active_runs["unknown-tenant:no-user"] = runs_mod._MAX_RUNS_PER_USER
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint()(request=_request(with_context=False), scheduler=FakeScheduler()))
    assert exc.value.status_code == 429


def test_release_user_run_unit():
    runs_mod._release_user_run("nobody")  # 从 0 释放：保持缺席
    assert "nobody" not in runs_mod._user_active_runs
    runs_mod._user_active_runs["u"] = 2
    runs_mod._release_user_run("u")
    assert runs_mod._user_active_runs["u"] == 1
    runs_mod._release_user_run("u")
    assert "u" not in runs_mod._user_active_runs
    runs_mod._release_user_run("u")  # 再释放：缺席保持
    assert "u" not in runs_mod._user_active_runs
