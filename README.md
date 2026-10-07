# zst-mcp · 智思体教育 Agent 平台自研模块集

BNU「智思体（GEST）」智能教育平台的工程化模块集。平台整体基于 **Dify 二次开发**，本仓库收录本人设计/实现的四个模块：

| 模块 | 内容 | 规模 | 协议/端口 |
|---|---|---|---|
| `usermem/` | 跨会话用户记忆服务（MCP + REST 双协议） | ~1200 行 Node.js | MCP `/sse`、`/mcp` + REST，8083 |
| `ppt-judge/` | PPT 评审 MCP（评审链路 + 标注图） | ~1000 行 Python | Streamable HTTP，8003 |
| `fogsight/` | 互动教学动画生成 MCP 封装层（引擎为开源改造，见下） | 封装层 ~700 行 Python | Streamable HTTP，8001 |
| `agents/` | 两个自设计智能体的完整配置与 31 版迭代快照 | 38 个文件 | — |

另有 `docs/`：平台 MCP 集群从旧服务器迁移到华为鲲鹏 ARM 内网机的部署记录（DEPLOY-STATUS.md / MIGRATION.md）与 pm2 进程配置，为真实运维过程留档。

## 架构位置

```
Web 前端（gest 前端机）
   │
Dify 平台（二次开发：dify-agent 运行时扩展 · pydantic-ai 分层架构 · 沙箱编排）
   │  MCP（Streamable HTTP / SSE）
   ├── usermem        跨会话记忆：偏好/技能/任务/交付物，按用户隔离存储 + cron 主动任务
   ├── ppt-judge      PPT 评审：relay → judge → wpss → LLM 评审，产出报告与标注图
   ├── fogsight-mcp   动画生成：教学概念 → 互动 HTML 动画（含 TTS 配音）
   └── 沙箱执行        local / e2b 多后端 + 租约（lease）调度
```

智能体（`agents/`）通过 Dify 工具协议绑定以上 MCP 提供的 28 个工具（记忆读写、定时任务、交付物归档、PPT 评审、动画生成、图表、检索等）。

## 各模块要点

- **usermem** — 同一服务暴露 MCP（供智能体调用）与 REST（供前端技能面板调用）双协议；`X-Usermem-Key` 鉴权；数据按用户目录隔离（preferences / skills / tasks / deliverables）；`scheduler.js` 实现定时主动任务（如学习提醒）。
- **ppt-judge** — 学生上传 PPT → LibreOffice 转换 → 逐页评审 → LLM 出报告与标注图；`server.py`（stdio 工具实现）与 `server_sse.py`（HTTP 传输层）分离。
- **fogsight** — `mcp_server.py` 将动画生成能力封装为 MCP 工具（含资源、批量 TTS 注入），另有平台 provider 切换、数据迁移等对接脚本。
- **agents** — 智思体综合（24 版，2026-09-11 → 09-29）与教师智能体（7 版）的 `config.json` 全量快照：system prompt、28 个工具绑定、模型参数（deepseek-v4.1-flash）、memory 范围、沙箱配置；`versions/` 保留每一次迭代的完整历史。

## 归属与出处声明

- `fogsight/app.py`、`app_3d.py`、`start_fogsight.py` 来自开源项目 **Fogsight.ai**（LICENSE：CC BY-NC-ND 4.0，随附于 `fogsight/LICENSE`）；本人贡献为 MCP 封装层（`mcp_server.py`）及平台对接/迁移脚本。仓库为私有、不分发，保留原 LICENSE 与出处以示尊重。
- 智能体配置快照导出自平台 Dify 数据库（`agents` / `agent_config_snapshots` 表），为本人迭代过程的真实记录。

## 安全与隐私

- 服务器上的真实凭据与用户数据**不入库**：`.env`（含 USERMEM_KEY）、`usermem/data/`（真实学生数据）、`fogsight/credentials.json` 均已排除，各服务提供 `.env.example` 模板。
- 历史快照中出现的旧企业微信 webhook 密钥已脱敏为 `<REDACTED>`。
- 内网 IP（RFC1918）保留以便还原部署上下文，无公网暴露面。

## 运行

每个模块目录内有独立说明与 `.env.example`；MCP 服务均可用 Dify「自定义 MCP 工具」以 Streamable HTTP 方式接入。
