# 沙箱资源治理与生命周期：一次生产事故的复盘与架构演进

> 本文记录 2026-10-12 的一次真实生产事故复盘，以及由它触发的沙箱架构演进：
> 从「多租户共享一个沙箱 + append-only 任务台账」演进到「单租户易耗沙箱容器 + E2B 式 pause/resume 生命周期」。
> 所有改动 patch/override 级留痕于 [`dify-mods/`](../../dify-mods/)，部署于自托管华为鲲鹏内网机。

## 数字速查

| 维度 | 数字 |
|---|---|
| 事故会话 | 51 分钟 / 108 轮；~86 轮起每条 shell 命令报错；无效重试 22 轮（86→108） |
| 触发上限 | 单轮 run shell job 台账上限 32（`_MAX_TRACKED_JOBS_PER_RUN`） |
| 修复方式 | 台账「完成即回收」——shell_run / shell_wait / shell_interrupt 三处观察点，done 即移出台账 |
| 沙箱形态 | 一租户一独立 docker 容器（`dify-sbx-<uuid>`），创建时落地限额：2 CPU / 2G 内存 / 256 进程数 |
| 生命周期 | release 即 pause（进程冻结、文件保留，等价 E2B `pause(keep_memory=True)`）；重连自动 resume；闲置两级回收：1h pause → 24h stop 释放内存（文件保留），销毁只走客户端 DELETE |
| 活跃容量 | 同时运行沙箱 ≤100（paused 不占名额）；home 快照 ≤1GB、按租户命名空间隔离 |
| 代码规模 | shell layer 覆盖文件 836 行（upstream 114 行）；Gateway 518 行；enterprise 客户端补丁 +42 行 + 单测 +69 行 |

## 治理链条总览

威胁模型统一假设「**终端用户不可信**」。事故前后的完整链条：

```
入口闸门（runs route override）        每用户并发 run ≤2（429），全局 ≤64
        ↓
执行配额（shell layer override）       每 Binding 磁盘配额 2GB（创建/续会话双时机 du 校验）
        ↓                              单轮未完成 shell job ≤32（完成即回收）
超时与截断（官方机制）                  用户工具 30s，bootstrap 放宽 300s
        ↓
生命周期回收（自托管 Gateway）          release 即 pause / 闲置 1h pause → 24h stop / 快照按租户隔离
```

## 事故复盘：51 分钟会话被自己的安全阀掐断

### 现象

一次长会话的第一轮跑了 **51 分钟、108 轮**。agent 干的活很重：workspace bootstrap 的 `npm install`、装 jsdom、跑真实 DOM 端到端测试、渲染截图——每条 shell 命令都会在本轮的 job 跟踪表里记一笔。跑到 **~86 轮**时，每条 `shell_run` 都开始返回：

```
shell job limit for this turn reached (32 tracked jobs);
wait for or interrupt existing jobs before starting more
```

此后 agent **白白重试了 22 轮**（86→108）：按错误信息建议去 `shell_wait` 等待、去 `shell_interrupt` 打断，但完成/打断的 job 都不会移出台账，上限永远不降。最后 agent 把它误读成「本轮文件通道已达上限」告知用户——`index.html` 也没有传上去（该消息 `message_files=0`）。新轮次任务预算自动重置，所以用户随后「发一下链接」一次就成功了。

### 根因：上限的语义与实现不符

`_MAX_TRACKED_JOBS_PER_RUN = 32` 是 9/25 加的本地安全补丁（配套每 Binding 2GB 磁盘配额），**原始意图是给单个 run 的 agent loop 设资源预算**：模型偶尔会陷入「无限干活」模式——反复跑命令、烧 token、占着沙箱，需要一个每 run 的硬上限让 loop 停得下来。在这个「限制本轮总工作量」的语义下，append-only 台账本来就是自洽实现。事后复盘才看清问题：**真正要防的失控（死循环海量起任务）是「并发」概念，而预算限的是「累计」——两个语义在加补丁那天就悄悄脱节了**：

1. **job 台账是 append-only 的**：只在轮次结束时整体清空，已完成的 job 不会被中途回收。于是 32 实际上是「本轮 shell 命令**累计总数**上限」，与任务是否还活着无关——预算语义本身自洽，但对动辄上百条命令的正常长任务（装依赖 + 跑测试 + 渲染产物的组合），这个预算定得太低。
2. **错误信息的补救建议是假的**：提示 wait/interrupt，但这两个操作都不改变台账。agent 忠实地执行建议，结果是被永久卡死——错误处理路径反而放大了事故（22 轮无效重试）。
3. **受影响任务类别很典型**：任何超过 32 条 shell 命令的长任务都会在半路被掐。短会话形态永远踩不到，所以此前的 18 项故障注入回归集（全部短会话）没有拦住它。

一句话：**安全阀按「预算」设计、以「累计」落地，而真正要防的失控是「水位」——语义错了层，错误信息还谎报了排水方法。**

### 修复：完成即回收（保持防失控语义）

最小改动原则：不删上限、不加新机制，只让台账反映真实水位——上限语义从「本轮总预算」重定义为「未完成并发上限」。

- **三处观察点回收**：`shell_run` / `shell_wait` / `shell_interrupt` 观察到 job `done=true`（结果已随本次 observation 交付）即移出台账；`shell_interrupt` 后仍在 grace 期未终止的 job 保持跟踪，等 `shell_wait` 观察终态。
- **上限语义重定义**：32 只统计**未完成**任务——32 个同时活跃的失控任务照样被挡住，长任务不再被误伤。取舍：改成并发语义后，「海量廉价命令」式的跑量不再被这个上限拦——但该类失控有别的兜底（单任务 300s 超时、磁盘配额、失败按周归因监控），跑量可见可加护栏，而误杀是用户直接可感的交付失败。
- **错误信息改为真话**：`32 unfinished jobs; wait for existing jobs to finish (shell_wait) or interrupt them (shell_interrupt)`——建议与系统行为一致。
- **prompt 契约同步**：shell 工具使用说明中加入「finished job is released from tracking once its result is returned; read the returned `output_path` with a new shell_run if you need its full output」——模型被告知续读截断输出的正确姿势（新起 `shell_run` cat `output_path`），不依赖已回收的 job。
- **未知 job 的报错也带解释**：`_ensure_tracked_job` 对已回收 job 报「Finished jobs are released from tracking once their result has been delivered; run a new command if you need more output」。

一个实现细节值得记录：模型状态对象开启了 `validate_assignment`，不变量 `job_offsets ⊆ job_ids` 在**每次字段赋值时**都会校验。因此回收顺序必须**先缩 `job_offsets` 再缩 `job_ids`**——两个中间态才都合法。反序会在第一个赋值处抛校验异常。

容器内的 job 记录与输出文件**不清理**：无害，随沙箱销毁一起回收。

## 架构演进：单租户易耗沙箱 + E2B 式生命周期

### 动机

事故之外，旧形态还有一个结构性问题：**多租户共享一个沙箱容器**。所有用户的 shell 任务跑在同一个文件系统命名空间里，隔离靠 shell layer 的 Binding 目录约定——对一个以「终端用户不可信」为威胁模型的系统，这是纵深防御链上最薄的一层。对照官方 E2B 的模型（每个沙箱是独立 VM/微虚拟机，一租户一实例，用完即弃），决定自托管一个 mini Gateway 把官方 enterprise 后端的契约实现出来。

核心设计承诺：**沙箱是易耗品，快照才是租户资产**。一个 Binding = 一个独立 docker 容器，CPU/内存/进程数限额在容器创建时落地；租户的持久状态通过 home 快照显式保存/恢复，容器本身随时可 pause/销毁重建。

### Gateway 契约（客户端代码即规格书）

`docker/gateway/gateway.py`（518 行，FastAPI + docker SDK）严格实现 `runtime_backend/enterprise.py` 客户端所期望的契约：

| 端点 | 语义 |
|---|---|
| `POST /v1/sandboxes` | 按租户创建沙箱容器（可带 `homeSnapshotRef` 恢复 home 目录）；活跃容量 100，超限 429 |
| `POST /v1/sandboxes/{id}/pause` | 客户端 release 路径：立即回收活跃资源。幂等 204 |
| `POST /v1/sandboxes/{id}/resume` | 客户端 acquire 前置恢复。幂等 204 |
| `POST /v1/sandboxes/{id}/home-snapshots` | 捕获 home 目录 tar+gzip 快照（≤1GB，413 拒绝），返回租户命名空间的 `snapshotRef` |
| `DELETE /v1/sandboxes/{id}` | 销毁沙箱（客户端容忍 404） |
| `DELETE /v1/home-snapshots/{ref}` | 删除快照。幂等 204 |
| `/proxy/*` | 反向代理到沙箱 shellctl（`X-Sandbox-Id` 路由），失败自动 resume 后重试一次 |

### 生命周期语义：对齐官方 E2B

- **release 即 pause**：`docker pause`（SIGSTOP 冻结进程）——文件保留、内存保留、CPU 归零，等价 E2B `pause(keep_memory=True)`。pause 不销毁，重连即恢复。
- **重连自动 resume**：acquire 路径先调 `/resume` 再建数据面（学官方 E2B connect 的自动恢复）；`/proxy` 请求失败时也 resume 后重试一次——被 pause 的沙箱对客户端几乎透明。
- **闲置两级回收**：第一档 `SBX_IDLE_PAUSE_SECONDS=3600`（与官方 `E2B_MAX_ACTIVE_TIMEOUT_SECONDS` 同款 1h）处理 release 未触达的情况（进程崩溃/网关丢调用），只 pause 不销毁。第二档 `SBX_IDLE_STOP_SECONDS=86400`（累计闲置 24h，含已 pause 时段）把 paused 沙箱 unpause 后 stop——pause 的 cgroup freezer 仍占内存，stop 才把 RAM 真正归还宿主机；文件保留在容器可写层，用户回来走既有 `/resume` 冷启动（~1-2s）。reaper 视野相应从 running 扩到 running+paused。销毁仍只走客户端 DELETE，reaper 永不销毁。
- **代理流量即心跳**：`/proxy` 每个请求入口刷新 `last_seen`（曾只在 resume 分支刷新——修复前持续超过 1h 的活跃会话水位线停在创建时刻，会被 reaper 误判闲置、run 中途被 pause 掐停）。
- **回收竞态防护**：reaper 判定与执行之间若 `last_seen` 变化（客户端刚 resume），本轮放弃、下轮再判——不误回收刚回来的沙箱。
- **容量口径**：活跃上限 100 只数 running 容器，paused/stopped 不占名额——pause/stop 正是容量回收的手段。
- **对齐客户端错误判定**：未知/已销毁沙箱返回 `404 + code=sandbox_not_found`，与客户端 `_is_missing_sandbox` 的 BindingLost 判定一致，租约状态机不会悬挂。

### 资源限额与安全细节

- **容器创建时落地限额**：`mem_limit=2G`、`nano_cpus=2`、`pids_limit=256`——单租户失控最多烧掉自己的份额，伤不到宿主机与其他租户。
- **快照租户命名空间隔离**：快照 ref 强制为 `{tenantId}.{homeSnapshotId}`，租户段禁止点号；不同租户传相同 snapshotId 不会互相覆盖/窃取。写入走 tmp+rename 原子替换。
- **双层鉴权 + 每沙箱独立 token**：外部只持 `X-Inner-Api-Key`；每个沙箱创建时注入独立的 shellctl token（`secrets.token_hex(32)`，学官方 E2B `traffic_access_token`），`/proxy` 按沙箱注入 Bearer——沙箱内不出现网关控制面密钥，被攻破的沙箱横向访问不了其他租户沙箱或网关 API。转发实现踩过一个真实坑：Starlette headers 迭代出的都是小写键，混用大小写键重建会出现两个 `Authorization`，shellctl 读到的是调用方的网关密钥导致 401——`/proxy` 现在显式剥离调用方 `Authorization` 后以小写键重建。
- **出网管控**：沙箱 HTTP(S)_PROXY 指向专用 SSRF 代理容器，与 Dify 官方沙箱共用出口策略。
- **重启不丢账**：Gateway 以 docker label（sandbox-id / tenant-id）登记，启动时按 label 对账重建注册表；强删 paused 容器前先 unpause（docker 拒绝删除 paused 容器）。

### 客户端侧补丁与测试

`enterprise.py`（+42 行）：`release` 在关闭数据面后立即 `POST pause`（失败仅告警，reaper 兜底）；`acquire` 重连路径先 `_resume_sandbox`（404 → BindingLost，其余 → BindingAcquire）。单测 +69 行覆盖：acquire 自动 resume 及失败映射、release 恰好发出一次带 `X-Inner-Api-Key` 的 pause。

### 部署验证

自托管华为鲲鹏内网机实测：Gateway 容器运行中；两个按租户创建的 `dify-sbx-<uuid>` 容器，其一在 release 后处于 `Paused` 状态——pause/resume 链路真实生效。两级闲置回收随 2026-10-12 镜像重建生效；24h 档是渐进兜底，生产暂无触发记录（预期行为：首次请求经 `/proxy` 自动 resume 冷启动恢复）。

## 关联

- 补丁与覆盖文件：[`dify-mods/README.md`](../../dify-mods/README.md)（patches ×11 / overrides ×2 / gateway）
- 底座架构研读：[`dify-agent-runtime.md`](dify-agent-runtime.md)
- 回归集：[`../../regress/`](../../regress/)（首批 9 补丁 + 2 override 的 18 项故障注入用例；本次修复由 dify-agent 自带单测覆盖）
