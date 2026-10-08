# regress · dify-mods 补丁回归集

把 `dify-mods/` 的 9 个补丁 + 2 个 override 转成可自动重放的断言用例，故障注入式验证补丁行为在位、可运行、防升级失效。

## 用例清单（18 项，首轮 ALL GREEN）

| 级别 | 用例 | 断言内容 | 对应补丁 |
|---|---|---|---|
| 功能 | T1 streak=8 熔断且熔断后静默 | 连续 8 次相同思考句触发熔断，后续 delta 不再产出 | `adapters/llm/model.py` |
| 功能 | T2 total=24 熔断（streak 被打断场景） | 交替内容打断 streak 后，累计 24 次仍触发 | 同上 |
| 功能 | T3 正常多样思考不误伤 | 40 句互异内容不触发熔断（无假阳性） | 同上 |
| 功能 | T4 单 delta 周期性复读熔断 | 单个 delta 内 9 段重复句子触发 periodic 分支 | 同上 |
| 功能 | T5 emit_run_failed 终态幂等 | 同一 run 重复 finalize 不追加重复终态事件 | `runtime/run_scheduler.py` + `event_sink.py` |
| 功能 | T6 非视觉模型正确识别 | schema 无 VISION 特性 → 判 False | `core/memory/token_buffer_memory.py` |
| 功能 | T7 视觉能力判定实例级缓存 | 3 次判定只查 1 次 schema（防逐条历史消息重复查询） | 同上 |
| 功能 | T8 schema 查询异常回退不误伤 | 查询异常 → 回退 True（保持附带文件，不误伤视觉模型） | 同上 |
| 功能 | T9 usermem 身份注入在位 | 系统提示词注入代码（用户标识/会话/end-user 判定）未随升级丢失 | `runtime_request_builder.py` |
| 在位 | 磁盘配额 2048MB / 双时机校验 | 挂载文件含配额常量与 `_enforce_binding_disk_quota` | `overrides/shell_layer.py` |
| 在位 | shell job 上限 32 / bootstrap 300s | 挂载文件含 `_MAX_TRACKED_JOBS_PER_RUN = 32`、300s | 同上 |
| 在位 | 并发闸门每用户 2 run / 429 | 挂载文件含 `_MAX_RUNS_PER_USER = 2`、429 | `overrides/runs_route.py` |
| 在位 | 复读熔断阈值 8/24 | 挂载文件含 `_THINK_REPEAT_*_LIMIT` | `adapters/llm/model.py` |
| 在位 | 工具产物 30MB 通道 | 挂载文件含 30MB 上限 | `layers/dify_plugin/tools_layer.py` |

「在位」断言针对 override 的最大风险：**Dify 升级后挂载路径/文件名变化导致覆盖静默失效**。

## 运行

```bash
# 本机同步到鲲鹏服务器后执行
rsync -aq --delete regress/ <internal-host>:~/regress/
ssh <internal-host> 'bash ~/regress/run.sh'
```

runner 通过 `docker exec -i` 把用例脚本送入 `docker-agent_backend-1` / `docker-api-1` 容器内直接执行（单元级，零额外依赖，不改容器）；override 在位断言在宿主机执行。聚合输出 `PASS/FAIL` 行，末尾 `REGRESS: ALL GREEN` / `FAILURES DETECTED`。

## 设计取舍

- **单元级优先**：直接 import 容器内挂载生效的补丁代码构造输入断言行为，不依赖模型调用，结果确定性 100%；
- **429 并发闸门的黑盒用例**（真实创建 3 个并发 run 断言第 3 个 429）暂不纳入自动集——需要真实 composition 与模型调用，作为手动验证项；
- **磁盘配额实际灌盘**（>2GB 触发拦截）同样留作手动验证，避免在共享宿主机上写大文件。
