#!/usr/bin/env bash
# 资源治理 override 挂载在位断言（在鲲鹏服务器上执行）。
# override 以 docker 挂载方式覆盖容器内官方文件，最大风险是 Dify 升级后挂载路径/文件名
# 变化导致 override 静默失效——本脚本断言关键常量仍在容器内生效文件中。
set -u
PASS=0; FAIL=0

check() {
  local name="$1" container="$2" pattern="$3" file="$4"
  if docker exec "$container" grep -q -- "$pattern" "$file" 2>/dev/null; then
    echo "PASS  $name"; PASS=$((PASS+1))
  else
    echo "FAIL  $name  -- 未找到: $pattern ($file)"; FAIL=$((FAIL+1))
  fi
}

LAYER=/app/api/.venv/lib/python3.12/site-packages/dify_agent/layers/shell/layer.py
RUNS=/app/api/.venv/lib/python3.12/site-packages/dify_agent/server/routes/runs.py
MODEL=/app/api/.venv/lib/python3.12/site-packages/dify_agent/adapters/llm/model.py
TOOLSL=/app/api/.venv/lib/python3.12/site-packages/dify_agent/layers/dify_plugin/tools_layer.py

check "磁盘配额常量 2048MB"      docker-agent_backend-1 "_BINDING_DISK_QUOTA_MB = 2048" "$LAYER"
check "配额 create/resume 双时机" docker-agent_backend-1 "_enforce_binding_disk_quota"   "$LAYER"
check "shell job 上限 32"        docker-agent_backend-1 "_MAX_TRACKED_JOBS_PER_RUN = 32" "$LAYER"
check "bootstrap 超时放宽 300s"  docker-agent_backend-1 "300"                            "$LAYER"
check "并发闸门每用户 2 run"     docker-agent_backend-1 "_MAX_RUNS_PER_USER = 2"         "$RUNS"
check "闸门 429 状态码"          docker-agent_backend-1 "429"                            "$RUNS"
check "复读熔断 streak 阈值 8"   docker-agent_backend-1 "_THINK_REPEAT_STREAK_LIMIT = 8" "$MODEL"
check "复读熔断 total 阈值 24"   docker-agent_backend-1 "_THINK_REPEAT_TOTAL_LIMIT = 24" "$MODEL"
check "工具产物 30MB 通道"       docker-agent_backend-1 "30"                             "$TOOLSL"

echo "total=$((PASS+FAIL)) failed=$FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
