"""Hard 桶：gateway 治理逻辑单测（假件、确定性、无真资源消耗）。

运行（.7 宿主机，或直接跑 docker/gateway/run_regression.sh）：
  docker cp docker/gateway/test_gateway_hard.py docker-agent_gateway-1:/tmp/
  docker exec docker-agent_gateway-1 python /tmp/test_gateway_hard.py

被测对象是容器内 /app/gateway.py（= 仓库 docker/gateway/gateway.py）。导入时会
执行真实 docker.from_env() + _reconcile()（容器内有 docker.sock，仅只读对账），
随后测试把 gateway.docker_client / _create_container_blocking /
_wait_shellctl_healthy 换成假件，不再触碰真实 docker 与真沙箱。

覆盖（对应 docker-compose.override.yaml agent_gateway 注释里的治理承诺）：
  1. create 容量闸门：运行中沙箱 >= SBX_MAX_ACTIVE -> 429 sandbox_capacity_reached；
     paused 不占活跃名额（同 5 个 paused，create 正常走通）。
  2. release->pause 幂等：连发两次 /pause 都是 204，只真正 pause 一次；
     /resume 恢复 + 刷新 last_seen；exited 沙箱 resume 走 start；
     未知沙箱 pause/resume 均 404 sandbox_not_found。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/app")

import docker  # noqa: E402

import gateway  # noqa: E402


# ---------------------------------------------------------------- 假件


class FakeContainer:
    def __init__(self, name: str, status: str = "running", labels: dict | None = None):
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.pause_calls = 0
        self.unpause_calls = 0
        self.start_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1
        self.status = "paused"

    def unpause(self) -> None:
        self.unpause_calls += 1
        self.status = "running"

    def start(self) -> None:
        self.start_calls += 1
        self.status = "running"


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []

    def list(self, all: bool = False, filters: dict | None = None) -> list[FakeContainer]:
        filters = filters or {}
        out = list(self.items)
        label = filters.get("label")
        if label:
            out = [c for c in out if c.labels.get(label)]
        status = filters.get("status")
        if status:
            out = [c for c in out if c.status == status]
        return out

    def get(self, name: str) -> FakeContainer:
        for c in self.items:
            if c.name == name:
                return c
        raise docker.errors.NotFound(f"container {name} not found")


class FakeDocker:
    def __init__(self, containers: FakeContainers) -> None:
        self.containers = containers
        self.created: list[dict] = []


class FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


def install_fakes(containers: FakeContainers) -> FakeDocker:
    client = FakeDocker(containers)
    gateway.docker_client = client
    gateway._registry.clear()
    return client


def fake_create_blocking(sandbox_id, tenant_id, snapshot_tar, shellctl_token):
    c = FakeContainer(f"dify-sbx-{sandbox_id}", status="running", labels={gateway.LABEL_SANDBOX: sandbox_id})
    gateway.docker_client.created.append({"id": sandbox_id, "tenant": tenant_id})
    gateway.docker_client.containers.items.append(c)
    return c


async def fake_wait_healthy(container) -> None:
    return None


# ---------------------------------------------------------------- 断言辅助

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def body_of(resp) -> dict:
    return json.loads(resp.body)


# ---------------------------------------------------------------- 用例


def test_capacity_gate_429() -> None:
    containers = FakeContainers()
    for i in range(2):
        containers.items.append(
            FakeContainer(f"c{i}", status="running", labels={gateway.LABEL_SANDBOX: f"s{i}", gateway.LABEL_TENANT: "t1"})
        )
    install_fakes(containers)
    gateway.SBX_MAX_ACTIVE = 2
    resp = asyncio.run(gateway.create_sandbox(FakeRequest({"tenantId": "t-hard"})))
    check("capacity: running==max -> 429", resp.status_code == 429, f"got {resp.status_code}")
    check(
        "capacity: 429 code sandbox_capacity_reached",
        body_of(resp).get("code") == "sandbox_capacity_reached",
        str(body_of(resp)),
    )
    check("capacity: 429 时不建容器", gateway.docker_client.created == [], str(gateway.docker_client.created))


def test_paused_do_not_count_toward_capacity() -> None:
    containers = FakeContainers()
    for i in range(5):
        containers.items.append(
            FakeContainer(f"p{i}", status="paused", labels={gateway.LABEL_SANDBOX: f"p{i}", gateway.LABEL_TENANT: "t1"})
        )
    install_fakes(containers)
    gateway.SBX_MAX_ACTIVE = 2
    gateway._create_container_blocking = fake_create_blocking
    gateway._wait_shellctl_healthy = fake_wait_healthy
    resp = asyncio.run(gateway.create_sandbox(FakeRequest({"tenantId": "t-hard"})))
    body = body_of(resp)
    check("paused 不占活跃名额: create 200", resp.status_code == 200, f"got {resp.status_code} {body}")
    sid = body.get("sandboxId", "")
    check("paused 不占活跃名额: 返回 sandboxId", bool(sid), str(body))
    check(
        "paused 不占活跃名额: 登记 tenant",
        gateway._registry.get(sid, {}).get("tenant") == "t-hard",
        str(gateway._registry.get(sid)),
    )


def test_release_pause_idempotent() -> None:
    containers = FakeContainers()
    sid = "sbx-hard-1"
    c = FakeContainer(f"dify-sbx-{sid}", labels={gateway.LABEL_SANDBOX: sid, gateway.LABEL_TENANT: "t-hard"})
    containers.items.append(c)
    install_fakes(containers)
    gateway._registry[sid] = {
        "name": c.name,
        "tenant": "t-hard",
        "created_at": "",
        "last_seen": time.monotonic(),
        "token": "tok",
    }
    r1 = asyncio.run(gateway.pause_sandbox(sid))
    r2 = asyncio.run(gateway.pause_sandbox(sid))
    check("release->pause 第一次 204", r1.status_code == 204, f"got {r1.status_code}")
    check("release->pause 第二次仍 204（幂等）", r2.status_code == 204, f"got {r2.status_code}")
    check("release->pause 只真正 pause 一次", c.pause_calls == 1, f"pause_calls={c.pause_calls}")
    check("release->pause 容器进入 paused", c.status == "paused", c.status)
    check("release 后登记保留（pause 不销毁）", sid in gateway._registry, "")

    r404 = asyncio.run(gateway.pause_sandbox("no-such-sandbox"))
    check("pause 未知沙箱 404", r404.status_code == 404, f"got {r404.status_code}")
    check("pause 未知沙箱 code sandbox_not_found", body_of(r404).get("code") == "sandbox_not_found", str(body_of(r404)))


def test_resume_recovers_and_refreshes_last_seen() -> None:
    containers = FakeContainers()
    sid = "sbx-hard-2"
    c = FakeContainer(f"dify-sbx-{sid}", status="paused", labels={gateway.LABEL_SANDBOX: sid})
    containers.items.append(c)
    install_fakes(containers)
    stale = time.monotonic() - 3600
    gateway._registry[sid] = {
        "name": c.name,
        "tenant": "t-hard",
        "created_at": "",
        "last_seen": stale,
        "token": "tok",
    }
    gateway._wait_shellctl_healthy = fake_wait_healthy
    err = asyncio.run(gateway._resume_sandbox(sid))
    check("resume paused 沙箱无错误", err is None, str(err))
    check("resume 调用 unpause", c.unpause_calls == 1, f"unpause_calls={c.unpause_calls}")
    check("resume 后回到 running", c.status == "running", c.status)
    check("resume 刷新 last_seen（防 reaper 误判）", gateway._registry[sid]["last_seen"] > stale, "")

    # exited 沙箱 resume 走 start（二级回收 stop 后的冷启动路径）
    c2 = FakeContainer(f"dify-sbx-{sid}", status="exited", labels={gateway.LABEL_SANDBOX: sid})
    containers.items = [c2]
    install_fakes(containers)
    gateway._registry[sid] = {"name": c2.name, "tenant": "t-hard", "created_at": "", "last_seen": stale, "token": "tok"}
    err2 = asyncio.run(gateway._resume_sandbox(sid))
    check("resume exited 沙箱无错误", err2 is None, str(err2))
    check("resume exited 调用 start", c2.start_calls == 1, f"start_calls={c2.start_calls}")

    install_fakes(FakeContainers())
    err404 = asyncio.run(gateway._resume_sandbox("no-such-sandbox"))
    check("resume 未知沙箱 404", err404 is not None and err404[0] == 404, str(err404))


def main() -> int:
    test_capacity_gate_429()
    test_paused_do_not_count_toward_capacity()
    test_release_pause_idempotent()
    test_resume_recovers_and_refreshes_last_seen()
    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
