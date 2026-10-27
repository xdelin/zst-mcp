# regress · dify-mods 补丁回归集

把 `dify-mods/` 的 9 个补丁 + 2 个 override 转成可自动重放的断言用例，故障注入式验证补丁行为在位、可运行、防升级失效。

## 用例清单（cases/，18 项，首轮 ALL GREEN）

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

## 三桶测试集（three-bucket/，沙箱治理）

`three-bucket/` 镜像服务器 dify 仓库 `docker/gateway` + `docker/patches` 的沙箱治理回归三桶，与上面 18 项互补——18 项覆盖 api / dify-agent 补丁本身，三桶覆盖**沙箱治理链路**（gateway + runs 闸门 + shell 层）：

| 桶 | 内容 | 运行时机 |
|---|---|---|
| easy | `gateway/regression.sh`：在位 9 项（容器/补丁挂载/环境注入/快照卷/双网络/健康页）+ 功能 10 项（401/create/proxy/pause 幂等/resume/stop 后自动恢复/销毁后 404；真栈单点调用，regression-easy 临时租户必清理） | 每次跑 |
| hard | `gateway/test_gateway_hard.py`（容量 429 / paused 不占名额 / release→pause 幂等 / resume 刷 last_seen，20 断言）+ `patches/test_runs_gate_hard.py`（每用户 2 / 全局 64 闸门、释放幂等、异常回滚、缺 context 落共享键，8 用例）+ `patches/test_shell_layer_hard.py`（配额脚本结构与触发、job 上限 32 不泄漏、完成即回收、台账不变量，11 用例）——全部假件、确定性，不碰真沙箱 | 每次跑/每夜 |
| black-box | `gateway/BLACKBOX.md`：真 3 并发 run（闸门+容量+release 生命周期全链路）、真 2GB 内存限额（磁盘配额顺带） | 升级节点手动 |

一键入口 `three-bucket/gateway/run_regression.sh`（easy + hard，退出码聚合；black-box 只打印提醒）。首轮实跑全绿：**easy 19 / gateway hard 20 / agent_backend hard 19**。hard 桶的两个关键回归形状：`test_limit_counts_only_unfinished_jobs` 即 51 分钟长会话误掐事故的复现（32 满→wait 回收→放行→再满→再拦）；闸门用例覆盖「拒绝不污染计数 / 双回调幂等 / 快完成同步释放 / 异常回滚」。

## 运行

```bash
# 本机同步到鲲鹏服务器后执行
rsync -aq --delete regress/ <internal-host>:~/regress/
ssh <internal-host> 'bash ~/regress/run.sh'
```

runner 通过 `docker exec -i` 把用例脚本送入 `docker-agent_backend-1` / `docker-api-1` 容器内直接执行（单元级，零额外依赖，不改容器）；override 在位断言在宿主机执行。聚合输出 `PASS/FAIL` 行，末尾 `REGRESS: ALL GREEN` / `FAILURES DETECTED`。

## 设计取舍

- **单元级优先**：直接 import 容器内挂载生效的补丁代码构造输入断言行为，不依赖模型调用，结果确定性 100%；
- **429 并发闸门**：决策逻辑已由三桶 hard 的假件单测覆盖（`test_runs_gate_hard.py`，预置台账 + 假调度器）；真 3 并发全链路接线留 black-box 桶（BLACKBOX.md 黑盒 1，升级节点手动）；
- **磁盘配额实际灌盘**（>2GB 触发拦截）留 black-box 桶（BLACKBOX.md 黑盒 2）；配额**决策逻辑**已由 hard 桶覆盖（假 commands 注入 exit_code，无需真 2GB 文件）。
