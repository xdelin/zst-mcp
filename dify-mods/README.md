# dify-mods · 基于 Dify 1.17.0 官方运行时的二次开发补丁

智思体平台基于 **Dify 1.17.0**（含其官方 `dify-agent/` agent server）部署。本目录收录本人在官方代码之上的本地修改——以 **patch（diff）形式**呈现，明确区分 upstream 基线与个人增量。

- `patches/` — 对 Dify 主 api 与 dify-agent 运行时的修改（`git diff` 导出，未回提交 upstream，通过 docker 挂载/本地运行生效）
- `overrides/` — 覆盖 dify-agent 镜像内文件的完整替换文件（挂载到容器内对应路径）

## patches/ 一览（合计 +545 行）

| 补丁 | 目标文件（upstream 1.17.0） | 规模 | 解决什么 |
|---|---|---|---|
| `runtime_request_builder` | `api/core/app/apps/agent_app/` | +29 | **跨会话记忆接线**：把 end-user 稳定标识（service API user 参数 / CAS 用户名）与会话 id 注入系统提示词，usermem MCP 工具据此把记忆路由到正确用户 |
| `token_buffer_memory` | `api/core/memory/` | +38 | 修复：生成过图片的会话在非视觉模型上逐条抛 dashscope 异常、**永久失败**。增加模型视觉能力检测（实例级缓存）+ 助手侧产物图片降级为文字注记 |
| `model` | `dify-agent/.../adapters/llm/model.py` | +145 | **思考复读熔断**：同一思考句连续 8 次/累计 24 次判为复读退化并截断；句读切片 + 无句读长片的尾部周期性检测 |
| `run_scheduler` | `dify-agent/.../runtime/run_scheduler.py` | +20 | run 在意外位置死亡时 SSE 静默结束、前端只见对话中断；补发 `emit_run_failed`（幂等，已有终态时 applied=False）转为用户可见错误 |
| `tools_layer` | `dify-agent/.../layers/dify_plugin/tools_layer.py` | +202 | **工具产物文件通道**：思维导图 PNG 等大文件上传 Dify 文件通道（30MB 上限对齐上传能力），相对 URL 防混合内容拦截，历史消息 URL 重签 |
| `runtime_support` | `api/core/workflow/nodes/agent/` | +86 | agent 节点注入用户上传文件的**长效下载 URL**（HMAC 签名），替代会过期的短时签名 |
| `mcp_tools_manage_service` | `api/services/tools/` | +22 | MCP provider 列表 `id` 字段为 server_identifier 而非 UUID 的兼容校验修复 |
| `test_layers` | `dify-agent/tests/.../test_layers.py` | +24 | 与 tools_layer 改动同步的测试 |
| `docker-compose` | `docker/docker-compose.yaml` | +2 | 沙箱内 pip 安装走内网镜像源（PIP_INDEX_URL / PIP_TRUSTED_HOST） |

## overrides/ 一览（资源治理）

覆盖 `dify-agent` 官方文件，威胁模型统一假设为「**终端用户不可信**」：

| 覆盖文件 | 对应 upstream | 核心改动 |
|---|---|---|
| `dify_agent_shell_layer.py` | `src/dify_agent/layers/shell/layer.py`（114→更名对照，+59 行差异） | ① 每个 Binding（home+workspace）**磁盘配额 2048MB**：`du -sm` 汇总校验，超限本轮 run 直接失败并给明确原因，context create / resume 双时机检查（防 dd/海量下载灌满宿主机）② **单轮 run shell job 数上限 32**（防模型失控海量起任务）③ workspace bootstrap（npm install 等）超时单独放宽 300s，用户工具默认 30s 不变 |
| `dify_agent_runs_route.py` | `src/dify_agent/server/routes/runs.py`（114 → 188 行） | **并发闸门**：每用户同时运行 run 数上限 2（超限 429）、全局限 64；done_callback 释放计数，异常路径同步回滚 |

与 `overrides` 配套的资源治理链条：**入口并发闸门（runs route）→ 执行中配额与 job 上限（shell layer）→ 超时与输出截断（官方机制 + bootstrap 放宽）**。

## 为什么用 patch 形式

底座是 Dify 官方 Apache-2.0 代码，本仓库只收录个人增量；diff 形式可精确审阅每处改动的动机与影响面。升级 Dify 版本时按文件逐个复核（`runtime_request_builder` 补丁头注释里写明了这一约定）。

平台底座架构的研读笔记见 [`../docs/architecture/dify-agent-runtime.md`](../docs/architecture/dify-agent-runtime.md)。
