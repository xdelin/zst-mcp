# black-box 桶（手动，升级 / 换线节点跑）

easy / hard 桶都用假件或单点真调用，本桶验证**全链路真接线**——真模型、真
agent_backend、真 gateway、真沙箱容器一起参与。共 2 项，每次升级节点（compose
大版本升级、gateway/runtime_backend 补丁重挂、镜像升级）后手动跑一遍。

前置：`.7` compose 全家桶运行中（`docker compose ps` 全部 Up）；浏览器可访问
gest 前端并有可用测试账号；`bash docker/gateway/regression.sh` 已绿。

---

## 黑盒 1：真 3 并发 run（runs 闸门 + gateway 容量 + release 生命周期）

目的：验证 per-user 并发闸门、每用户计数独立、done_callback 释放、client
release → pause 的全链路行为（这些在 hard 桶里只是假件逻辑）。

步骤：

1. 账号 A 连开两个会话，各发一个长任务 run（例：
   `用 shell_run 执行 sleep 240 然后输出 done`，确保 run 持续几分钟）。
2. 同账号 A 再发第三个 run → 应立刻失败，错误文案含
   `per-user concurrent agent run limit`（agent_backend 429 透传）。
3. 账号 B 发一个 run → 应正常执行（每用户计数独立）。
4. 等账号 A 任意一个 run 结束后，再发一个 run → 应正常（done_callback 释放名额）。
5. 关闭账号 A 的页面（客户端 release 触达）→ 几秒内对应沙箱容器转 paused：
   `docker ps --filter name=dify-sbx- --format '{{.Names}} {{.Status}}'`
6. 重新打开页面继续对话 → 沙箱自动 resume（acquire 前置 /resume），上下文仍在。

验证点：

```bash
docker logs docker-agent_backend-1 2>&1 | grep -i "per-user concurrent"   # 步骤 2 的 429
docker exec docker-agent_gateway-1 python -c \
  "import httpx; print(httpx.get('http://localhost:8100/healthz').json())"
# sandboxes/running 数应与 docker ps 里 label=dify-agent-gateway.sandbox-id 的
# running 容器数一致；paused 不占 running 名额
```

## 黑盒 2：真 2GB 内存限额（+ 2GB 磁盘配额顺带验证）

目的：验证 per-tenant 容器限额真的落在 cgroup 上（hard 桶只验证了配置面）。

步骤（先经 gateway 建一个专用沙箱；`$SID` 为返回的 sandboxId）：

```bash
GW=http://agent_gateway:8100
KEY=$(sed -n 's/^AGENT_GATEWAY_INNER_API_KEY=//p' docker/.env | head -n1)
docker run --rm -i -e KEY="$KEY" curlimages/curl:latest -s \
  -X POST http://agent_gateway:8100/v1/sandboxes -H "X-Inner-Api-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"tenantId":"regression-blackbox"}'
```

（网络不便时也可从 agent_backend 容器内用 python3+httpx 仿照 regression.sh。）

1. 内存限额在位：
   `docker inspect dify-sbx-$SID --format '{{.HostConfig.Memory}}'` 应为
   `2147483648`（2g）；`{{.HostConfig.NanoCpus}}` 应为 `2000000000`（2 CPU）；
   `{{.HostConfig.PidsLimit}}` 应为 `256`。
2. 超限进程被杀、容器本体存活：经 /proxy 在沙箱里跑
   `python3 -c "x=bytearray(2500*1024*1024)"` → 进程被 cgroup OOM kill
   （exit 137 / MemoryError），随后再跑 `echo ok` 仍 200（容器未被误杀）。
3. 磁盘配额触发：沙箱里 `dd if=/dev/zero of=$HOME/big bs=1M count=2500`，
   之后**新开一轮 run**（触发 on_context_resume/create 的配额检查）→ run 失败，
   错误含 `Binding disk quota exceeded`；沙箱删除后重建恢复正常。
4. 清理：`DELETE /v1/sandboxes/$SID` → 204，`docker ps -a` 里无残留容器。

注：gateway /proxy 对 **paused** 沙箱的上游 connect 要等满 120s 超时才走自动
resume（设计如此，客户端 proxy_timeout=60s 会先超时），所以手动验证自动恢复
用 stop 过的容器（veth 已拆，秒级报错触发 resume）——regression.sh 功能8 即此路径。
