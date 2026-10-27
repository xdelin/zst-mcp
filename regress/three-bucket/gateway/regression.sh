#!/usr/bin/env bash
# easy 桶：沙箱治理在位断言 + 功能断言（秒级、确定性、每次跑；真栈只读 + 一个临时沙箱）。
#
#   用法: bash docker/gateway/regression.sh          （在 .7 仓库根，或任意目录）
#   退出码: 0 = 全部通过；1 = 有失败
#
# 内容（各 9 项）：
#   在位断言 —— 部署面（容器/补丁/环境/网络/卷）是否按 compose 期望就位；
#   功能断言 —— 对真 gateway 走一遍 create/proxy/pause/resume/delete 生命周期，
#   用 regression-easy 租户，结束必清理（包括中途失败时）。
#
# 密钥：只在运行时从 docker/.env 抽取 AGENT_GATEWAY_INNER_API_KEY 一行注入
# docker exec 环境；绝不写入本脚本、绝不 echo、绝不进 git（docker/.env 不提交）。
set -u
cd "$(dirname "$0")/../.."

GW=docker-agent_gateway-1
BE=docker-agent_backend-1
GW_URL=http://agent_gateway:8100
SHELL_PATCH=/app/api/.venv/lib/python3.12/site-packages/dify_agent/layers/shell/layer.py
RUNS_PATCH=/app/api/.venv/lib/python3.12/site-packages/dify_agent/server/routes/runs.py

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1  ${2:-}"; }

# 汇总容器内 python 断言程序的输出行（PASS x / FAIL x）进总账：
# python 的退出码在 $( ) 里拿不到，只认行首标记。
tally() {
  local line
  while IFS= read -r line; do
    case "$line" in
      PASS*) PASS=$((PASS+1)) ;;
      FAIL*) FAIL=$((FAIL+1)) ;;
    esac
  done <<< "$1"
}

# ---- 密钥（不打印） ----
GW_KEY=$(sed -n 's/^AGENT_GATEWAY_INNER_API_KEY=//p' docker/.env | head -n1)
GW_KEY=${GW_KEY%$'\r'}; GW_KEY=${GW_KEY%\"}; GW_KEY=${GW_KEY#\"}
if [ -z "$GW_KEY" ]; then bad "docker/.env 提供 AGENT_GATEWAY_INNER_API_KEY" "key missing"; fi

have() { [ "$1" = "0" ]; }

# ================= 在位断言 =================

docker ps --filter "name=^$GW$" --filter status=running -q | grep -q . \
  && ok "在位1 agent_gateway 容器运行中" || bad "在位1 agent_gateway 容器运行中"

docker exec "$BE" printenv DIFY_AGENT_RUNTIME_BACKEND 2>/dev/null | grep -qx enterprise \
  && ok "在位2 agent_backend DIFY_AGENT_RUNTIME_BACKEND=enterprise" || bad "在位2 agent_backend DIFY_AGENT_RUNTIME_BACKEND=enterprise" "$(docker exec "$BE" printenv DIFY_AGENT_RUNTIME_BACKEND 2>/dev/null)"

docker exec "$BE" grep -q "完成即回收" "$SHELL_PATCH" 2>/dev/null \
  && ok "在位3 shell 层补丁已挂载（完成即回收标记）" || bad "在位3 shell 层补丁已挂载（完成即回收标记）" "$SHELL_PATCH"

docker exec "$BE" grep -q "_MAX_RUNS_PER_USER" "$RUNS_PATCH" 2>/dev/null \
  && ok "在位4 runs 并发闸门补丁已挂载" || bad "在位4 runs 并发闸门补丁已挂载" "$RUNS_PATCH"

docker exec "$GW" printenv SBX_IMAGE 2>/dev/null | grep -q . \
  && ok "在位5 gateway SBX_IMAGE 已注入" || bad "在位5 gateway SBX_IMAGE 已注入"

[ "$(docker exec "$GW" printenv SBX_IDLE_PAUSE_SECONDS 2>/dev/null)" = "3600" ] \
  && ok "在位6 一级闲置回收 SBX_IDLE_PAUSE_SECONDS=3600" || bad "在位6 一级闲置回收 SBX_IDLE_PAUSE_SECONDS=3600" "$(docker exec "$GW" printenv SBX_IDLE_PAUSE_SECONDS 2>/dev/null)"

[ -d docker/volumes/agent_gateway/snapshots ] \
  && ok "在位7 快照卷目录存在" || bad "在位7 快照卷目录存在" "docker/volumes/agent_gateway/snapshots"

docker network inspect docker_agent_sandbox_network docker_local_sandbox_proxy_network >/dev/null 2>&1 \
  && ok "在位8 沙箱双网络存在" || bad "在位8 沙箱双网络存在"

HEALTH_JSON=$(docker exec "$BE" python3 -c "
import httpx
r = httpx.get('$GW_URL/healthz', timeout=10.0, trust_env=False)
print(r.status_code); print(r.text)
" 2>/dev/null)
H_STATUS=$(echo "$HEALTH_JSON" | sed -n 1p)
H_BODY=$(echo "$HEALTH_JSON" | sed -n 2p)
if [ "$H_STATUS" = "200" ] && echo "$H_BODY" | grep -q '"sandboxes"' && echo "$H_BODY" | grep -q '"running"'; then
  ok "在位9 gateway /healthz 200 且含 sandboxes/running 字段"
else
  bad "在位9 gateway /healthz 200 且含 sandboxes/running 字段" "status=$H_STATUS body=$H_BODY"
fi

# ================= 功能断言（阶段 1） =================
# 输出约定：最后一行 SID=<id>（create 成功时），供 bash 阶段接管容器。
OUT1=$(docker exec -i -e GW_KEY="$GW_KEY" -e GW_URL="$GW_URL" "$BE" python3 - <<'PYEOF'
import os, sys, httpx

KEY, BASE = os.environ["GW_KEY"], os.environ["GW_URL"]
failed: list[str] = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        failed.append(name)

c = httpx.Client(base_url=BASE, headers={"X-Inner-Api-Key": KEY},
                 timeout=httpx.Timeout(90.0, connect=10.0), trust_env=False)
sid = None
try:
    try:
        r = httpx.post(BASE + "/v1/sandboxes", json={"tenantId": "regression-easy"},
                       timeout=10.0, trust_env=False)
        check("功能1 无密钥 create -> 401", r.status_code == 401, f"got {r.status_code}")
    except Exception as exc:
        check("功能1 无密钥 create -> 401", False, repr(exc))

    r = c.post("/v1/sandboxes", json={"tenantId": "regression-easy"})
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    sid = body.get("sandboxId") if r.status_code == 200 else None
    check("功能2 create -> 200 + sandboxId", bool(sid), f"got {r.status_code} {r.text[:200]}")

    r = c.post("/v1/sandboxes", json={"tenantId": "regression-easy",
                                      "homeSnapshotRef": "regression-easy.no-such-snapshot"})
    check("功能3 未知快照 create -> 404 snapshot_not_found",
          r.status_code == 404 and r.json().get("code") == "snapshot_not_found",
          f"got {r.status_code} {r.text[:200]}")

    if sid:
        h = {"X-Sandbox-Id": sid}
        r = c.get("/proxy/healthz", headers=h)
        check("功能4 proxy(X-Sandbox-Id) shellctl -> 200", r.status_code == 200, f"got {r.status_code}")
        r = c.post(f"/v1/sandboxes/{sid}/pause")
        check("功能5 release pause -> 204", r.status_code == 204, f"got {r.status_code}")
        r = c.post(f"/v1/sandboxes/{sid}/pause")
        check("功能6 pause 幂等 -> 仍 204", r.status_code == 204, f"got {r.status_code}")
        r = c.post(f"/v1/sandboxes/{sid}/resume")
        check("功能7 resume -> 204", r.status_code == 204, f"got {r.status_code}")
finally:
    # 交还给 bash 做停机续跑；这里只在异常且还没交出时兜底清理
    if sid and failed:
        try:
            c.delete(f"/v1/sandboxes/{sid}")
            sid = None
        except Exception:
            pass
print(f"SID={sid or ''}")
sys.exit(1 if failed else 0)
PYEOF
) || true
echo "$OUT1"
tally "$OUT1"
SID=$(echo "$OUT1" | sed -n 's/^SID=//p' | tail -n1)

# ================= 功能断言（阶段 2：stop 后 proxy 自动恢复 + 销毁回收） =================
if [ -n "$SID" ]; then
  docker stop "dify-sbx-$SID" >/dev/null 2>&1 || true
  OUT2=$(docker exec -i -e GW_KEY="$GW_KEY" -e GW_URL="$GW_URL" "$BE" python3 - <<PYEOF
import os, httpx

KEY, BASE = os.environ["GW_KEY"], os.environ["GW_URL"]
failed = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        failed.append(name)

c = httpx.Client(base_url=BASE, headers={"X-Inner-Api-Key": KEY},
                 timeout=httpx.Timeout(90.0, connect=10.0), trust_env=False)
sid = "$SID"
h = {"X-Sandbox-Id": sid}
try:
    r = c.get("/proxy/healthz", headers=h)
    check("功能8 容器 stop 后 proxy 自动恢复 -> 200", r.status_code == 200, f"got {r.status_code}")
    r = c.delete(f"/v1/sandboxes/{sid}")
    check("功能9 destroy -> 204", r.status_code == 204, f"got {r.status_code}")
    r = c.get("/proxy/healthz", headers=h)
    check("功能9b destroy 后 proxy -> 404 sandbox_not_found",
          r.status_code == 404 and r.json().get("code") == "sandbox_not_found",
          f"got {r.status_code} {r.text[:200]}")
    sid = None
finally:
    if sid:
        try:
            c.delete(f"/v1/sandboxes/{sid}")
        except Exception:
            pass
raise SystemExit(1 if failed else 0)
PYEOF
) || true
  echo "$OUT2"
  tally "$OUT2"
else
  echo "FAIL 功能8 容器 stop 后 proxy 自动恢复 -> 200  no sandbox id (阶段1失败)"
  echo "FAIL 功能9 destroy -> 204  no sandbox id (阶段1失败)"
  echo "FAIL 功能9b destroy 后 proxy -> 404 sandbox_not_found  no sandbox id (阶段1失败)"
  FAIL=$((FAIL+3))
fi

echo
echo "easy 桶: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
