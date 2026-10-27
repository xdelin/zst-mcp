#!/usr/bin/env bash
# 沙箱治理回归三桶入口。
#
#   bash docker/gateway/run_regression.sh
#
#   easy 桶     在位 9 + 功能 10（真栈单点调用，秒级）        —— 每次跑
#   hard 桶     gateway 治理 20 断言（gateway 容器内假件）
#               runs 闸门 8 用例 + shell 层 11 用例（agent_backend 容器内 pytest）
#                                                          —— 每次跑（或每夜）
#   black-box   真 3 并发 run / 真 2GB 限额（手动，见 BLACKBOX.md）—— 升级节点
#
# 退出码：0 = easy+hard 全绿；1 = 有失败。black-box 只打印提醒，不计入退出码。
set -u
cd "$(dirname "$0")/../.."
RC=0

echo "==================== easy 桶（在位 + 功能断言）===================="
bash docker/gateway/regression.sh || RC=1
echo

echo "==================== hard 桶：gateway 治理单测（假件）===================="
docker cp docker/gateway/test_gateway_hard.py docker-agent_gateway-1:/tmp/test_gateway_hard.py &&
  docker exec -u root docker-agent_gateway-1 chmod 644 /tmp/test_gateway_hard.py &&
  docker exec docker-agent_gateway-1 python /tmp/test_gateway_hard.py || RC=1
echo

echo "==================== hard 桶：runs 闸门 + shell 层补丁单测（pytest）===================="
docker cp docker/patches/test_runs_gate_hard.py docker-agent_backend-1:/tmp/test_runs_gate_hard.py &&
  docker cp docker/patches/test_shell_layer_hard.py docker-agent_backend-1:/tmp/test_shell_layer_hard.py &&
  docker exec -u root docker-agent_backend-1 chmod 644 /tmp/test_runs_gate_hard.py /tmp/test_shell_layer_hard.py &&
  docker exec docker-agent_backend-1 python -m pytest -q /tmp/test_runs_gate_hard.py /tmp/test_shell_layer_hard.py || RC=1
echo

echo "==================== black-box 桶（手动，升级节点）===================="
echo "两项手动用例见 docker/gateway/BLACKBOX.md：真 3 并发 run / 真 2GB 内存限额。"
echo
[ "$RC" = "0" ] && echo "easy + hard 全绿" || echo "存在失败（退出码 $RC）"
exit $RC
