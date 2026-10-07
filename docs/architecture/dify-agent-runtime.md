# Dify Agent Server（dify-agent）架构研读笔记

> 性质声明：`dify-agent/` 是 **Dify 1.17.0 官方**引入的独立 agent server（Apache-2.0，见 langgenius/dify 上游，2026-05 起进入主干）。本文是本人基于源码研读与生产环境运维整理的**理解笔记**，不是本人编写的代码——写作目的是支撑对平台底座的讲解，以及说明个人补丁（见 `dify-mods/`）各自作用在哪一层。

## 1. 定位与部署形态

- Dify 主 api（Flask，`api/`）负责应用编排、会话、模型供应商、知识库等；`dify-agent/` 是从主站中拆出的**独立 agent 执行引擎**，负责 agent loop 的实际运行。
- 生产形态：`langgenius/dify-agent-backend:1.17.0` 镜像独立成容器，与主 api 通过 HTTP（`server/routes/runs.py` 暴露 run 提交/事件流）+ SSE 交互；本地修改通过 docker 挂载覆盖镜像内文件或 `docker-compose.e2b.yaml` 自构建镜像（`DIFY_AGENT_RUNTIME_BACKEND: e2b`）。
- 代码组织（`dify-agent/src/dify_agent/`）：`layers/`（分层能力）、`runtime_backend/`（沙箱后端）、`agent_stub/`（控制面）、`storage/`（run 状态）、`runtime/`（调度）、`server/`（HTTP 面）、`adapters/`（LLM/shell 适配）。

## 2. 分层模型（layers）

agent 的一次运行由若干 layer 组合（composition）而成：

- **model layer**（`adapters/llm/model.py` 包装）：对接模型供应商，处理流式输出、思考段（thinking）与工具调用事件的转换。
- **tools layer**（`layers/dify_plugin/tools_layer.py`）：把 Dify 侧的插件工具与 MCP 工具桥接进 agent loop——工具声明、调用、结果回填都在这一层；工具产物文件的回传通道也在此实现（我们的 30MB 上传补丁落点）。
- **shell layer**（`layers/shell/layer.py`）：面向沙箱内 shell 工具的执行层——工作区 bootstrap、`shell_run/shell_wait/shell_input` 工具语义、超时（默认 30s）与终止宽限（10s）、输出截断（上下 edge bytes 保留策略）。

层与层通过 execution context 传递配置（`layers/execution_context/`），composition 里每层带 `type` + `config`。

## 3. 沙箱运行时（runtime_backend）

- 多后端抽象：`local`（本机/容器沙箱）、`e2b`（云端沙箱）等，由 `DIFY_AGENT_RUNTIME_BACKEND` 选择；`shellctl.py` 是 shell 会话的底层控制通道。
- **lease（租约）机制**：沙箱资源按租约分配与回收，支撑会话挂起/恢复（context create → resume）而资源不泄漏。
- 本地沙箱配套容器：`local_sandbox`（执行体）+ `agent_ssrf_proxy`（squid，出口代理——防 SSRF，沙箱内对外访问走代理白名单）。

## 4. run 生命周期与事件流

```
Dify 主 api                    dify-agent server                 沙箱
─────────────                  ─────────────────                 ─────
POST /runs ──────────────────▶ server/routes/runs.py
                                runtime/run_scheduler.py
                                  创建 runner（pydantic-ai loop）
                                  model/tools/shell layers ────▶ shellctl ─▶ local/e2b
                                  ◀── 事件（thinking/ tool / message）
                                sink 聚合事件 ────────────────▶ SSE 流
◀── SSE 转发（message/error）
前端渲染
```

- run 状态经 `storage/redis_run_store.py` **外置到 Redis**：进程重启不丢 run、可多实例横向扩展；会话快照（session snapshot）随 run 持久化，支撑次日续聊（context resume）。
- 异常路径：runner 正常失败会 emit `run_failed`；但 run 在意外位置死掉（或 emit 本身失败）时 SSE 会**静默结束**——我们的 `run_scheduler` 补丁在此补发幂等的 failed 事件（`dify-mods/patches/`）。

## 5. 资源治理（官方机制 + 我们的补丁）

| 环节 | 机制 | 来源 |
|---|---|---|
| 入口 | 每用户并发 run 闸门（2）+ 全局上限（64），超限 429，done_callback 释放 | 本地补丁（runs route override） |
| 磁盘 | 每 Binding home+workspace 2GB 配额，`du -sm` 校验，create/resume 双时机 | 本地补丁（shell layer override） |
| 进程 | 单轮 run shell job 上限 32 | 本地补丁（shell layer override） |
| 时间 | shell 工具 30s 超时 + 10s 终止宽限；bootstrap 单独放宽 300s | 官方 + 本地补丁 |
| 输出 | 输出截断（edge bytes 策略）防上下文爆炸 | 官方 |
| 网络 | agent_ssrf_proxy 出口代理 | 官方 |

## 6. 与 Dify 主 api 的接缝（我们补丁的另一落点）

- `runtime_request_builder.py`：主 api 组装 run 请求处——我们把 end-user 稳定标识与会话 id 注入系统提示词，让 agent 内的 usermem MCP 工具能路由到正确用户（跨会话记忆的「最后一公里」）。
- `token_buffer_memory.py`：主 api 的历史消息组装——多模态历史与视觉模型的兼容处理。
- `runtime_support.py`：agent 节点的文件引用解析与长效下载 URL 注入。

## 7. 研读收获（面试视角）

1. **拆分动机**：agent loop 从主 api 拆出独立服务，换来的是独立扩缩容、run 状态外置后的重启安全、以及沙箱后端可插拔。
2. **分层的好处**：model/tools/shell 各层可独立替换与测试（官方自带 layer 测试，我们的 tools_layer 补丁同步改了测试）。
3. **不可信用户假设**：所有资源治理补丁基于同一威胁模型——终端用户不可信，入口限并发、执行限磁盘与 job 数、出口走 SSRF 代理，纵深防御。
