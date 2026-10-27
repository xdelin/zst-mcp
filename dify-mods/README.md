# dify-mods · 基于 Dify 1.17.0 官方运行时的二次开发补丁

智思体平台基于 **Dify 1.17.0**（含其官方 `dify-agent/` agent server）部署。本目录收录本人在官方代码之上的本地修改——以 **patch（diff）形式**呈现，明确区分 upstream 基线与个人增量。

- `patches/` — 对 Dify 主 api 与 dify-agent 运行时的修改（`git diff` 导出，未回提交 upstream，通过 docker 挂载/本地运行生效）
- `overrides/` — 覆盖 dify-agent 镜像内文件的完整替换文件（挂载到容器内对应路径）
- `gateway/` — 自托管 E2B 式沙箱 Gateway（独立服务，非对官方文件的修改）

## patches/ 一览（合计 +881/−30 行，12 文件）

| 补丁 | 目标文件（upstream 1.17.0） | 规模 | 解决什么 |
|---|---|---|---|
| `runtime_request_builder` | `api/core/app/apps/agent_app/` | +26/−3 | **跨会话记忆接线**：把 end-user 稳定标识（service API user 参数 / CAS 用户名）与会话 id 注入系统提示词，usermem MCP 工具据此把记忆路由到正确用户 |
| `token_buffer_memory` | `api/core/memory/` | +38 | 修复：生成过图片的会话在非视觉模型上逐条抛 dashscope 异常、**永久失败**。增加模型视觉能力检测（实例级缓存）+ 助手侧产物图片降级为文字注记 |
| `model` | `dify-agent/.../adapters/llm/model.py` | +191/−16 | **思考复读熔断 + 结构化 thinking**：同一思考句连续 8 次/累计 24 次判为复读退化并截断（句读切片 + 无句读长片尾部周期性检测）；思考-only 轮注入兜底正文防整轮报废；对声明原生 reasoning_content 的供应商（默认 tongyi，环境变量可扩）注入 `thinking_output=structured`，按 chunk 字段存在性判定通路，思考增量送入与标签机**共用**的熔断器 |
| `test_model` | `dify-agent/tests/.../adapters/llm/test_model.py` | +165/−1 | 与 model 改动同步的单测：结构化思考 bypass 标签解析、结构化+工具调用混排、tongyi 白名单注入参数（provider 归一化被请求断言覆盖）、非白名单不注入 |
| `run_scheduler` | `dify-agent/.../runtime/run_scheduler.py` | +19/−1 | run 在意外位置死亡时 SSE 静默结束、前端只见对话中断；补发 `emit_run_failed`（幂等，已有终态时 applied=False）转为用户可见错误 |
| `tools_layer` | `dify-agent/.../layers/dify_plugin/tools_layer.py` | +201/−1 | **工具产物文件通道**：思维导图 PNG 等大文件上传 Dify 文件通道（30MB 上限对齐上传能力），相对 URL 防混合内容拦截，历史消息 URL 重签 |
| `runtime_support` | `api/core/workflow/nodes/agent/` | +85/−1 | agent 节点注入用户上传文件的**长效下载 URL**（HMAC 签名），替代会过期的短时签名 |
| `mcp_tools_manage_service` | `api/services/tools/` | +20/−2 | MCP provider 列表 `id` 字段为 server_identifier 而非 UUID 的兼容校验修复 |
| `test_layers` | `dify-agent/tests/.../test_layers.py` | +23/−1 | 与 tools_layer 改动同步的测试 |
| `enterprise` | `dify-agent/.../runtime_backend/enterprise.py` | +42 | **沙箱生命周期客户端侧**：release 即 `POST pause`（失败仅告警，闲置 reaper 兜底）；acquire 重连先自动 resume（404 → BindingLost）——对齐官方 E2B connect 语义 |
| `test_enterprise` | `dify-agent/tests/.../test_enterprise_backend.py` | +69 | 与 enterprise 改动同步的单测：resume 失败映射、release 恰好一次带鉴权头的 pause |
| `docker-compose` | `docker/docker-compose.yaml` | +2 | 沙箱内 pip 安装走内网镜像源（PIP_INDEX_URL / PIP_TRUSTED_HOST） |

## overrides/ 一览（资源治理 + 结构化 thinking 载体）

以完整替换文件覆盖容器内官方文件——agent_backend 挂载补丁（`dify_agent_*`）、模型运行时实体（`graphon_*`）、插件文件（`tongyi/`），威胁模型统一假设为「**终端用户不可信**」：

| 覆盖文件 | 对应 upstream | 核心改动 |
|---|---|---|
| `dify_agent_shell_layer.py` | `src/dify_agent/layers/shell/layer.py`（114→重命名扩展，+90 行差异） | ① 每个 Binding（home+workspace）**磁盘配额 2048MB**：`du -sm` 汇总校验，超限本轮 run 直接失败并给明确原因，context create / resume 双时机检查（防 dd/海量下载灌满宿主机）② **单轮 run 未完成 shell job 数上限 32**（防模型失控海量起任务）——**完成即回收**：job done 即移出台账，append-only 台账曾把 51 分钟/108 轮长会话在半路误掐（agent 空转 22 轮），复盘见 [治理文档](../docs/architecture/sandbox-governance.md) ③ workspace bootstrap（npm install 等）超时单独放宽 300s，用户工具默认 30s 不变 |
| `dify_agent_runs_route.py` | `src/dify_agent/server/routes/runs.py`（114 → 188 行） | **并发闸门**：每用户同时运行 run 数上限 2（超限 429）、全局限 64；done_callback 释放计数，异常路径同步回滚 |
| `graphon_llm_entities.py` | `graphon/model_runtime/entities/llm_entities.py` | `LLMResultChunkDelta` / `LLMResult` 增加 `reasoning_content` 流式字段（非流式已有，流式此前无处承载）——结构化思考的**协议载体**，适配器靠它的字段存在性判定通路；升级 graphon 时对照复核 |
| `tongyi/`（`llm.py` + `sdk_llm_entities.py`） | plugin_daemon 卷 `langgenius/tongyi-0.2.21@…/models/llm/llm.py` 等 | 插件收到 `thinking_output=structured`（`pop` 消费，不透传 DashScope）时切换原生 `reasoning_content` 通路：每个下发 chunk 都携带该字段（无思考为 `""`），遗留 `<think>` 包装路径保留。**版本定版 0.2.21**，插件升级需重新比对覆盖 |

与 `overrides` 配套的资源治理链条：**入口并发闸门（runs route）→ 执行中配额与未完成任务上限（shell layer）→ 超时与输出截断（官方机制 + bootstrap 放宽）→ 生命周期回收（gateway pause/resume + 两级闲置回收：1h pause → 24h stop）**。

### 结构化 thinking 端到端（修法 B）

**问题**：思考模型把推理以 `<think>` 文本标签混在正文流里下发。标签是运行时合成的传输编码，包装状态机按「轮」作用而思考块天然跨轮（interleaved thinking：轮末裸开标签后去调工具、草稿断在半路），流式协议边界一泄漏就产生孤儿闭标签——正文复读 + 脏标签（实勘两种泄漏形态，铁证见 regress 与防守手册 Q2.4b）。

**方案坐标**：API 层业界已收敛结构化思考字段（OpenAI reasoning items / Claude thinking blocks / DeepSeek·豆包·Kimi·Qwen reasoning_content），文本标签只活在 vLLM 推理解析层。两个候选：轮调度层跨轮思考状态机（保留文本编码、补切片语义）vs 字段端到端透传。选后者——删掉整类显示层伪影的成因，而不是在下游继续追标签。

**四件套协作（上下游契约）**：

1. `tongyi/` 插件：消费 `thinking_output=structured`（`pop`，不透传 DashScope），切换 `reasoning_content` 原生通路——**每个下发 chunk 必带该字段**（无思考为 `""`），遗留通路恒 `None`；
2. `graphon_llm_entities.py`：流式 delta 增加 `reasoning_content` 协议字段；
3. `model` 适配器：请求侧按供应商白名单注入参数（provider 三段归一化，环境变量可扩）；响应侧按 **chunk 字段存在性**判定通路（无跨 chunk 状态、不依赖顺序），思考增量送入与 `<think>` 标签机**共用**的复读熔断器；
4. 下游零改动：ThinkingPart 从运行时到存储 / SSE / 前端的原链路不变；前端 think.ts 标签修复降级为非结构化通路的兜底。

**验证**：agent_backend 容器内 23 单测全过（4 新增：bypass 标签解析 / 与工具调用混排 / 白名单注入含 provider 归一化断言 / 非白名单不注入）；部署接线实证——model.py bind mount 进容器、tongyi 0.2.21 与补丁文件逐字节一致（0.2.18/0.2.20 未覆盖，插件升级需重打）。熔断共享保证新通路不引入第二类复读事故。

## gateway/（自托管 E2B 式沙箱 Gateway）

`gateway/gateway.py`（518 行，FastAPI + docker SDK）+ `Dockerfile`。把官方 enterprise 后端所期望的沙箱契约在自托管环境实现出来：**一租户一独立 docker 容器**（创建时落地 2 CPU / 2G 内存 / 256 进程数限额），**release 即 pause**（进程冻结、文件保留，等价 E2B `pause(keep_memory=True)`）、重连自动 resume、**两级闲置回收**（1h pause → 24h stop 释放内存，文件保留、销毁只走客户端 DELETE，reaper 带 resume 竞态防护）、每沙箱独立 shellctl token（沙箱内不出现网关控制面密钥）、home 快照 ≤1GB 按租户命名空间隔离、`/proxy` 反向代理失败自动恢复重试。契约、安全细节与部署验证见 [`../docs/architecture/sandbox-governance.md`](../docs/architecture/sandbox-governance.md)。

## 为什么用 patch 形式

底座是 Dify 官方 Apache-2.0 代码，本仓库只收录个人增量；diff 形式可精确审阅每处改动的动机与影响面。升级 Dify 版本时按文件逐个复核（`runtime_request_builder` 补丁头注释里写明了这一约定）。

平台底座架构的研读笔记见 [`../docs/architecture/dify-agent-runtime.md`](../docs/architecture/dify-agent-runtime.md)。
