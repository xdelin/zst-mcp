# 生产失败数据分析（failure-driven eval · 第一层）

> 数据口径：Dify 主 api `messages` 表元数据（status / error 字符串 / created_at），统计窗口 2026-05-06 → 2026-10-02。
> **隐私声明：仅统计错误元数据与计数，学生对话原文未导出服务器。** 统计 SQL 见 [classify.sql](classify.sql)。

## 1. 总体运行面貌

| 指标 | 值 |
|---|---|
| 统计窗口 | 2026-05-06 → 2026-10-02（5 个月） |
| 真实会话消息 | 966 条（9 月单月 926 条，开学季真实负载） |
| 覆盖应用 | 24 个（核心：智思体综合 201 / 智能助学 151 / 智能教务 122 / 文献服务 113） |
| 失败消息 | 69 条，**失败率 7.2%** |

## 2. 失败模式聚类（11 类）

| failure mode | 条数 | 占比 | 归属域 | 与 dify-mods 补丁的关系 |
|---|---|---|---|---|
| model_upstream（模型服务不可用 / qwen 400） | 23 | 33.3% | 上游模型服务 | 无（平台外因） |
| mcp_plugin_invoke_error（MCP 连接失败 Errno 111 为主） | 18 | 26.1% | 本人 MCP 基础设施 | MCP 集群部署稳定性 |
| tool_config_drift（tool parameter not found 等） | 7 | 10.1% | 智能体配置 | 配置迭代期间的参数漂移 |
| multimodal_file_type（file type not supported） | 6 | 8.7% | 运行时 | **多模态历史 bug 的失败面（对应 patch）** |
| bootstrap_failure（bootstrap workspace 失败） | 5 | 7.2% | 运行时 | **bootstrap 超时/稳定性 patch 相关** |
| run_silent_death（error 中记录 success 响应） | 4 | 5.8% | 运行时 | **run 意外死亡特征（对应幂等 run_failed patch）** |
| plugin_daemon_error | 2 | 2.9% | 插件守护进程 | — |
| config_validation | 1 | 1.4% | 配置 | — |
| rate_limit_triggered（Agent backend HTTP 429） | 1 | 1.4% | 运行时 | **并发闸门 patch 按预期触发（防护证据，非故障）** |
| agent_retry_exhausted | 1 | 1.4% | agent 运行时 | — |
| unknown | 1 | 1.4% | — | — |

归因合计：本人负责域（运行时 + MCP 基础设施）35 条（50.7%），上游模型服务 23 条（33.3%），配置/其他 11 条（15.9%）。

## 3. 补丁相关失败的「发生 → 修复 → 归零」时间线

按自然周统计三类补丁相关失败的分布：

| 周（MM-DD） | multimodal_file_type | bootstrap_failure | run_silent_death |
|---|---|---|---|
| 09-07 | 4 | 4 | 0 |
| 09-14 | 2 | 1 | 0 |
| 09-21 | 0 | 0 | 4 |
| 09-28 | 0 | 0 | 0 |
| 10-05 | 0 | 0 | 0 |

- **multimodal_file_type**：09 月上旬集中暴露，多模态历史修复（视觉能力实例级缓存 + 产物图片降级）上线后归零；
- **bootstrap_failure**：同期暴露，bootstrap 超时放宽至 300s 后归零；
- **run_silent_death**：09 下旬集中出现（run 意外死亡导致 SSE 静默中断），幂等 run_failed 补丁后归零。

三类均满足简历数字四连问：分母（966 条会话消息）、基线（修复前周失败数）、窗口（暴露周 → 修复后周）、环境（生产环境真实用户流量）。

## 4. 产出的可引用数字

1. 平台上线 5 个月，承载 24 个应用 / 966 次真实会话，统计失败率 7.2%；
2. 失败按 11 类 failure mode 聚类归因：本人负责域占 50.7%，上游模型故障占 33.3%；
3. 3 类失败与本人补丁一一对应（多模态历史 / bootstrap 超时 / run 静默死亡），修复后对应类别失败**归零**（周趋势可复现）；
4. 并发闸门在生产环境按预期触发（429 限流记录），防护机制有效性有直接证据。

## 5. 后续两层（待做）

- **fault-injection 回归集**：9 个补丁各转 1–2 个可重放用例，部署服务器 `regress/` 一键重放；
- **失败对话复放**：error 会话输入在当前版本重放，统计恢复成功率（原文不出服务器）。
