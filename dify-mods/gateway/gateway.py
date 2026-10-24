"""Dify Agent Enterprise Gateway（自托管 mini 实现）.

严格实现 dify_agent/runtime_backend/enterprise.py 客户端所期望的契约
（客户端代码即规格书）：

  POST   /v1/sandboxes                      {"tenantId","homeSnapshotRef"?} -> {"sandboxId"}
  POST   /v1/sandboxes/{id}/pause           Client release 立即回收活跃资源（幂等 204）
  POST   /v1/sandboxes/{id}/resume          Client acquire 前恢复（幂等 204）
  POST   /v1/sandboxes/{id}/home-snapshots  {"tenantId","agentId","homeSnapshotId"} -> {"snapshotRef"}
  DELETE /v1/home-snapshots/{ref}           幂等 204
  DELETE /v1/sandboxes/{id}                 客户端容忍 404（absent_status=404）
  /proxy/*                                   反向代理到沙箱 shellctl（X-Sandbox-Id 路由）

核心承诺：一个 Binding = 一个独立 docker 容器——CPU/内存/进程数限额在容器
创建时落地（per-tenant 隔离），快照是租户资产，沙箱是易耗品。
每沙箱独立 shellctl token（学官方 E2B traffic_access_token）：Gateway 建容器时
生成随机 token 注入并在 /proxy 按沙箱注入 Bearer；沙箱内不出现网关控制面密钥
（X-Inner-Api-Key），防被攻破的沙箱横向访问其他租户沙箱或网关 API。
生命周期学官方 E2B：release 即 pause（进程冻结、文件保留、不占 CPU，等价
E2B pause(keep_memory=True)），重新接入自动恢复；SBX_IDLE_PAUSE_SECONDS
（官方同款 1h = E2B_MAX_ACTIVE_TIMEOUT_SECONDS）只是 release 未触达时的
防泄漏兜底，pause 不销毁。二级回收：闲置 ≥ SBX_IDLE_STOP_SECONDS（默认 24h，
含已 pause 的沙箱）被 stop 释放内存——文件仍保留，/resume 冷启动（~1-2s）恢复，
销毁仍只走客户端 DELETE。未知/已销毁沙箱的代理请求返回
404 + code=sandbox_not_found，与客户端 _is_missing_sandbox 的 BindingLost 判定对齐。
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import docker
import httpx
from docker.models.containers import Container
from docker.utils import parse_bytes
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")

SBX_IMAGE = os.environ.get("SBX_IMAGE", "langgenius/dify-agent-local-sandbox:1.17.0")
SBX_NETWORKS = [n.strip() for n in os.environ.get("SBX_NETWORKS", "agent_sandbox_network,local_sandbox_proxy_network").split(",") if n.strip()]
SBX_SHELLCTL_PORT = int(os.environ.get("SBX_SHELLCTL_PORT", "5004"))
SBX_CPUS = float(os.environ.get("SBX_CPUS", "2"))
SBX_MEMORY = os.environ.get("SBX_MEMORY", "2g")
SBX_PIDS = int(os.environ.get("SBX_PIDS", "256"))
SBX_MAX_ACTIVE = int(os.environ.get("SBX_MAX_ACTIVE", "100"))
SBX_SNAPSHOT_MAX_MB = int(os.environ.get("SBX_SNAPSHOT_MAX_MB", "1024"))
SBX_START_TIMEOUT = float(os.environ.get("SBX_START_TIMEOUT", "60"))
# 学官方 E2B：闲置超时只 pause 不销毁（on_timeout="pause"），默认 3600s 即官方
# E2B_MAX_ACTIVE_TIMEOUT_SECONDS。仅作 release 未触达（进程崩溃/网关丢调用）的兜底。
SBX_IDLE_PAUSE_SECONDS = float(os.environ.get("SBX_IDLE_PAUSE_SECONDS", "3600"))
# 二级回收：闲置超过该秒数的挂起沙箱被 stop 释放内存（文件保留，下次 /resume
# 冷启动 ~1-2s；到这个深度的后台 job 早已弃置）。默认 24h，防弃用会话渐近占满内存。
SBX_IDLE_STOP_SECONDS = float(os.environ.get("SBX_IDLE_STOP_SECONDS", "86400"))
SBX_HTTP_PROXY = os.environ.get("SBX_HTTP_PROXY", "http://agent_ssrf_proxy:3128")
GATEWAY_TOKEN = os.environ.get("GATEWAY_INNER_API_KEY", "")
SNAPSHOT_ROOT = Path(os.environ.get("SBX_SNAPSHOT_ROOT", "/snapshots"))
LABEL_SANDBOX = "dify-agent-gateway.sandbox-id"
LABEL_TENANT = "dify-agent-gateway.tenant-id"
_PROXY_STRIP = "/proxy"

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "host", "content-length"}

docker_client = docker.from_env()
app = FastAPI(title="dify-agent enterprise gateway")

_registry: dict[str, dict[str, Any]] = {}
proxy_client: httpx.AsyncClient | None = None


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if GATEWAY_TOKEN and request.url.path not in ("/healthz",):
            if request.headers.get("X-Inner-Api-Key") != GATEWAY_TOKEN:
                return _err(401, "unauthorized", "missing or invalid X-Inner-Api-Key")
        return await call_next(request)


app.add_middleware(AuthMiddleware)


def _check_ref(value: str, what: str) -> str:
    if not _REF_RE.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _check_tenant(value: str) -> str:
    # 组合快照 ref 为 "{tenantId}.{homeSnapshotId}"，租户段禁止点号以消除歧义
    _check_ref(value, "tenantId")
    if "." in value:
        raise ValueError("tenantId must not contain '.'")
    return value


def _container_name(sandbox_id: str) -> str:
    return f"dify-sbx-{sandbox_id}"


def _shellctl_token(container: Container) -> str:
    """Recover a sandbox's own shellctl token from its container env."""
    for entry in (container.attrs.get("Config", {}) or {}).get("Env") or []:
        if isinstance(entry, str) and entry.startswith("SHELLCTL_AUTH_TOKEN="):
            return entry.split("=", 1)[1]
    return GATEWAY_TOKEN


def _reconcile() -> None:
    """Boot 后按 label 对账：Gateway 重启不丢沙箱登记。"""
    _registry.clear()
    for c in docker_client.containers.list(all=True, filters={"label": LABEL_SANDBOX}):
        sid = c.labels.get(LABEL_SANDBOX)
        if sid:
            _registry[sid] = {
                "name": c.name,
                "tenant": c.labels.get(LABEL_TENANT, ""),
                "created_at": c.attrs.get("Created", ""),
                "last_seen": time.monotonic(),
                "token": _shellctl_token(c),
            }
    logger.info("reconciled %d live sandboxes", len(_registry))


def _get_container(sandbox_id: str) -> Container:
    info = _registry.get(sandbox_id)
    if info is None:
        raise KeyError(sandbox_id)
    return docker_client.containers.get(info["name"])


def _running_count() -> int:
    """SBX_MAX_ACTIVE 口径：同时运行中的沙箱数（paused 不占活跃名额）。"""
    return len(docker_client.containers.list(filters={"label": LABEL_SANDBOX, "status": "running"}))


def _destroy_container_blocking(container: Container) -> None:
    # docker 拒绝删除 paused 容器，先恢复再强删
    if container.status == "paused":
        try:
            container.unpause()
        except Exception:
            pass
    container.remove(force=True)


def _create_container_blocking(
    sandbox_id: str, tenant_id: str, snapshot_tar: bytes | None, shellctl_token: str
) -> Container:
    name = _container_name(sandbox_id)
    env = {"SHELLCTL_AUTH_TOKEN": shellctl_token}
    if SBX_HTTP_PROXY:
        env.update({"HTTP_PROXY": SBX_HTTP_PROXY, "HTTPS_PROXY": SBX_HTTP_PROXY, "NO_PROXY": "localhost,127.0.0.1"})
    host_config = docker_client.api.create_host_config(
        mem_limit=parse_bytes(SBX_MEMORY),
        nano_cpus=int(SBX_CPUS * 1e9),
        pids_limit=SBX_PIDS,
        network_mode=SBX_NETWORKS[0] if SBX_NETWORKS else "bridge",
    )
    container = docker_client.api.create_container(
        image=SBX_IMAGE,
        name=name,
        labels={LABEL_SANDBOX: sandbox_id, LABEL_TENANT: tenant_id},
        hostname=f"sbx-{sandbox_id[:12]}",
        environment=env,
        host_config=host_config,
    )
    container_obj = docker_client.containers.get(container["Id"])
    for net in SBX_NETWORKS[1:]:
        docker_client.networks.get(net).connect(container_obj)
    container_obj.start()
    if snapshot_tar is not None:
        container_obj.put_archive("/home", snapshot_tar)
    return container_obj


async def _wait_shellctl_healthy(container: Container) -> None:
    url = f"http://{container.name}:{SBX_SHELLCTL_PORT}/healthz"
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        deadline = SBX_START_TIMEOUT
        waited = 0.0
        while waited < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
            waited += 0.5
    raise TimeoutError(f"sandbox {container.name} shellctl not healthy in {deadline}s")


def _reap_once() -> None:
    """两级闲置回收：运行沙箱闲置 ≥ SBX_IDLE_PAUSE_SECONDS 先 pause；
    闲置 ≥ SBX_IDLE_STOP_SECONDS（含已 pause 的）stop 释放内存（文件保留）。"""
    now = time.monotonic()
    for c in docker_client.containers.list(all=True, filters={"label": LABEL_SANDBOX}):
        sid = c.labels.get(LABEL_SANDBOX)
        if not sid:
            continue
        status = c.status
        if status not in ("running", "paused"):
            continue
        info = _registry.get(sid)
        if info is None:
            _registry[sid] = {
                "name": c.name,
                "tenant": c.labels.get(LABEL_TENANT, ""),
                "created_at": c.attrs.get("Created", ""),
                "last_seen": now,
                "token": _shellctl_token(c),
            }
            continue
        last = info.get("last_seen")
        if not isinstance(last, float):
            info["last_seen"] = now
            continue
        idle = now - last
        if idle < SBX_IDLE_PAUSE_SECONDS:
            continue
        # 判定与执行之间客户端可能刚 resume：last_seen 变了就放弃本轮，下轮再判
        if info.get("last_seen") != last:
            continue
        if idle >= SBX_IDLE_STOP_SECONDS:
            logger.info("reaper: stopping idle sandbox %s (idle %.0fs) to release memory", sid, idle)
            try:
                if status == "paused":
                    c.unpause()
                c.stop(timeout=10)
            except Exception:
                logger.exception("reaper: failed to stop sandbox %s", sid)
        elif status == "running":
            logger.info("reaper: pausing idle sandbox %s (idle %.0fs)", sid, idle)
            try:
                c.pause()
            except Exception:
                logger.exception("reaper: failed to pause sandbox %s", sid)


async def _reaper() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await run_in_threadpool(_reap_once)
        except Exception:
            logger.exception("idle reaper failed")


def _capture_home_blocking(container: Container) -> tuple[bytes, int]:
    stream, _stat = container.get_archive("/home/dify", chunk_size=1 << 20)
    buf = io.BytesIO()
    max_bytes = SBX_SNAPSHOT_MAX_MB << 20
    total = 0
    for chunk in stream:
        total += len(chunk)
        if total > max_bytes:
            raise OverflowError(f"home snapshot exceeds {SBX_SNAPSHOT_MAX_MB}MB")
        buf.write(chunk)
    return buf.getvalue(), total


async def _resume_sandbox(sandbox_id: str) -> tuple[int, str] | None:
    """Ensure the sandbox can serve again; return (status, message) when it cannot."""
    try:
        container = await run_in_threadpool(_get_container, sandbox_id)
        status = await run_in_threadpool(lambda: container.status)
        if status == "running":
            return None
        if status == "paused":
            logger.info("resuming paused sandbox %s", sandbox_id)
            await run_in_threadpool(container.unpause)
        elif status in ("exited", "created"):
            logger.info("starting stopped sandbox %s (status=%s)", sandbox_id, status)
            await run_in_threadpool(container.start)
        else:
            return 404, f"sandbox {sandbox_id} is {status}"
        await _wait_shellctl_healthy(container)
    except (KeyError, docker.errors.NotFound):
        return 404, f"sandbox {sandbox_id} does not exist"
    except Exception as exc:
        logger.exception("resume of sandbox %s failed", sandbox_id)
        return 502, f"sandbox {sandbox_id} could not be resumed: {exc}"
    info = _registry.get(sandbox_id)
    if info is not None:
        info["last_seen"] = time.monotonic()
    return None


@app.on_event("startup")
async def _startup() -> None:
    global proxy_client
    _reconcile()
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    proxy_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0), trust_env=False)
    app.state.reaper = asyncio.create_task(_reaper())


@app.on_event("shutdown")
async def _shutdown() -> None:
    reaper = getattr(app.state, "reaper", None)
    if reaper is not None:
        reaper.cancel()
    if proxy_client is not None:
        await proxy_client.aclose()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    running = await run_in_threadpool(_running_count)
    return {"status": "ok", "sandboxes": len(_registry), "running": running}


@app.post("/v1/sandboxes")
async def create_sandbox(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        tenant_id = _check_tenant(str(body.get("tenantId", "default")))
    except ValueError as exc:
        return _err(400, "invalid_request", str(exc))
    except Exception:
        return _err(400, "invalid_request", "body must be a JSON object")
    snapshot_ref = body.get("homeSnapshotRef")
    snapshot_tar: bytes | None = None
    if snapshot_ref is not None:
        snapshot_ref = _check_ref(str(snapshot_ref), "homeSnapshotRef")
        if not snapshot_ref.startswith(f"{tenant_id}."):
            return _err(404, "snapshot_not_found", f"home snapshot {snapshot_ref} does not exist")
        path = SNAPSHOT_ROOT / f"{snapshot_ref}.tgz"
        if not path.is_file():
            return _err(404, "snapshot_not_found", f"home snapshot {snapshot_ref} does not exist")
        snapshot_tar = await run_in_threadpool(lambda: gzip.decompress(path.read_bytes()))
    if await run_in_threadpool(_running_count) >= SBX_MAX_ACTIVE:
        return _err(429, "sandbox_capacity_reached", f"active sandbox limit ({SBX_MAX_ACTIVE}) reached, retry later")

    sandbox_id = uuid.uuid4().hex
    # 每沙箱独立 shellctl token：与网关控制面密钥无关，防横向（学 E2B traffic_access_token）
    shellctl_token = secrets.token_hex(32)
    try:
        container = await run_in_threadpool(
            _create_container_blocking, sandbox_id, tenant_id, snapshot_tar, shellctl_token
        )
        await _wait_shellctl_healthy(container)
    except OverflowError:
        return _err(413, "snapshot_too_large", f"home snapshot exceeds {SBX_SNAPSHOT_MAX_MB}MB")
    except ValueError as exc:
        return _err(400, "invalid_request", str(exc))
    except Exception as exc:
        logger.exception("sandbox creation failed")
        def _cleanup() -> None:
            try:
                docker_client.containers.get(_container_name(sandbox_id)).remove(force=True)
            except Exception:
                pass
        await run_in_threadpool(_cleanup)
        return _err(502, "sandbox_create_failed", str(exc))
    _registry[sandbox_id] = {
        "name": container.name,
        "tenant": tenant_id,
        "created_at": "",
        "last_seen": time.monotonic(),
        "token": shellctl_token,
    }
    logger.info("sandbox %s created for tenant %s (snapshot=%s)", sandbox_id, tenant_id, snapshot_ref)
    return JSONResponse({"sandboxId": sandbox_id})


@app.post("/v1/sandboxes/{sandbox_id}/pause")
async def pause_sandbox(sandbox_id: str) -> Response:
    """Client release：立即回收活跃资源（学官方 E2B release→pause(keep_memory=True)）。

    进程冻结、文件保留、不占 CPU；下次 /proxy 失败或 /resume 自动恢复。幂等。
    """
    try:
        container = await run_in_threadpool(_get_container, sandbox_id)
        status = await run_in_threadpool(lambda: container.status)
    except (KeyError, docker.errors.NotFound):
        return _err(404, "sandbox_not_found", f"sandbox {sandbox_id} does not exist")
    if status == "running":
        try:
            await run_in_threadpool(container.pause)
        except docker.errors.NotFound:
            return Response(status_code=204)
        logger.info("sandbox %s paused (client release)", sandbox_id)
    return Response(status_code=204)


@app.post("/v1/sandboxes/{sandbox_id}/resume")
async def resume_sandbox(sandbox_id: str) -> Response:
    """Client acquire 前置恢复（学官方 E2B connect 自动 resume）。幂等。"""
    error = await _resume_sandbox(sandbox_id)
    if error is not None:
        status, message = error
        return _err(status, "sandbox_not_found" if status == 404 else "resume_failed", message)
    return Response(status_code=204)


@app.post("/v1/sandboxes/{sandbox_id}/home-snapshots")
async def create_snapshot(sandbox_id: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        tenant_id = _check_tenant(str(body.get("tenantId", "")))
        snapshot_id = _check_ref(str(body.get("homeSnapshotId", "")), "homeSnapshotId")
        # 快照文件按租户命名空间隔离：不同租户传相同 homeSnapshotId 不会互相覆盖/窃取
        snapshot_ref = f"{tenant_id}.{snapshot_id}"
    except ValueError as exc:
        return _err(400, "invalid_request", str(exc))
    try:
        container = await run_in_threadpool(_get_container, sandbox_id)
    except (KeyError, docker.errors.NotFound):
        return _err(404, "sandbox_not_found", f"sandbox {sandbox_id} does not exist")
    try:
        tar_bytes, total = await run_in_threadpool(_capture_home_blocking, container)
    except OverflowError as exc:
        return _err(413, "snapshot_too_large", str(exc))
    except Exception as exc:
        logger.exception("snapshot capture failed")
        return _err(502, "snapshot_capture_failed", str(exc))
    path = SNAPSHOT_ROOT / f"{snapshot_ref}.tgz"
    tmp = path.with_suffix(".tmp")
    await run_in_threadpool(lambda: (tmp.write_bytes(gzip.compress(tar_bytes)), tmp.rename(path)))
    logger.info("snapshot %s captured from %s (%.1fMB)", snapshot_ref, sandbox_id, total / 1e6)
    return JSONResponse({"snapshotRef": snapshot_ref})


@app.delete("/v1/home-snapshots/{snapshot_ref:path}")
async def delete_snapshot(snapshot_ref: str) -> Response:
    try:
        ref = _check_ref(snapshot_ref.split("/")[-1], "snapshotRef")
    except ValueError as exc:
        return _err(400, "invalid_request", str(exc))
    path = SNAPSHOT_ROOT / f"{ref}.tgz"
    await run_in_threadpool(lambda: path.unlink(missing_ok=True))
    return Response(status_code=204)


@app.delete("/v1/sandboxes/{sandbox_id}")
async def delete_sandbox(sandbox_id: str) -> Response:
    try:
        container = await run_in_threadpool(_get_container, sandbox_id)
    except (KeyError, docker.errors.NotFound):
        return _err(404, "sandbox_not_found", f"sandbox {sandbox_id} does not exist")
    await run_in_threadpool(_destroy_container_blocking, container)
    _registry.pop(sandbox_id, None)
    logger.info("sandbox %s destroyed", sandbox_id)
    return Response(status_code=204)


@app.api_route("/proxy/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(rest: str, request: Request) -> Response:
    sandbox_id = request.headers.get("X-Sandbox-Id", "")
    info = _registry.get(sandbox_id)
    if info is None:
        return _err(404, "sandbox_not_found", f"unknown sandbox {sandbox_id!r}")
    # 代理流量即心跳：每个到达的请求都刷新水位线。否则 last_seen 停在创建/恢复
    # 时刻，持续超过 1h 的活跃会话会被 reaper 误判闲置，在 run 中途被 pause 掐停。
    info["last_seen"] = time.monotonic()
    target = f"http://{info['name']}:{SBX_SHELLCTL_PORT}/{rest}"
    if request.url.query:
        target += f"?{request.url.query}"
    # 剥掉调用方的 Authorization（企业客户端带的是网关密钥，不能透传给沙箱），
    # 再以小写键重建——Starlette headers 迭代出的都是小写，混用大小写键会出现
    # 两个 Authorization，shellctl 读到的是调用方的那个导致 401。
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS and k.lower() != "authorization"
    }
    # 沙箱内 shellctl 的 Bearer 由 Gateway 按沙箱注入独立 token：企业客户端只持
    # X-Inner-Api-Key，不感知 shellctl token；沙箱内也没有网关控制面密钥。
    shellctl_token = info.get("token") or GATEWAY_TOKEN
    if shellctl_token:
        headers["Authorization"] = f"Bearer {shellctl_token}"
    body = await request.body()
    assert proxy_client is not None
    try:
        upstream = await proxy_client.request(
            request.method, target, headers=headers, content=body, follow_redirects=False
        )
    except httpx.HTTPError as exc:
        # 沙箱可能被 pause（release/闲置兜底）或意外退出：能恢复就恢复后重试一次，
        # 已销毁则 404 对齐客户端 _is_missing_sandbox 的 BindingLost 判定。
        logger.warning("proxy to %s failed: %s", sandbox_id, exc)
        resume_error = await _resume_sandbox(sandbox_id)
        if resume_error is not None:
            status, message = resume_error
            return _err(status, "sandbox_not_found" if status == 404 else "sandbox_unreachable", message)
        try:
            upstream = await proxy_client.request(
                request.method, target, headers=headers, content=body, follow_redirects=False
            )
        except httpx.HTTPError as exc2:
            logger.warning("proxy to %s failed after resume: %s", sandbox_id, exc2)
            return _err(502, "sandbox_unreachable", f"sandbox {sandbox_id} is unreachable")
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_HEADERS}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)


_reconcile()
