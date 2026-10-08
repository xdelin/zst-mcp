#!/usr/bin/env bash
# 在鲲鹏服务器上执行全部回归用例并聚合结果。
# 用法：bash run.sh   （本目录需已同步到服务器 ~/regress/）
set -u
cd "$(dirname "$0")"
FAILED=0

run_python_case() {
  local container="$1" case_file="$2"
  echo "### $case_file @ $container"
  if docker exec -i "$container" python3 - < "$case_file"; then
    :
  else
    FAILED=1
  fi
}

echo "# dify-mods 回归集 $(date '+%F %T')"
run_python_case docker-agent_backend-1 cases/test_agent_backend.py || FAILED=1
run_python_case docker-api-1 cases/test_api_patches.py || FAILED=1
echo "### cases/test_overrides_inplace.sh @ host"
bash cases/test_overrides_inplace.sh || FAILED=1

if [ "$FAILED" -eq 0 ]; then
  echo "REGRESS: ALL GREEN"
else
  echo "REGRESS: FAILURES DETECTED"
fi
exit $FAILED
