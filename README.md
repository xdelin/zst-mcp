# zst-mcp · 智思体教育 Agent 平台：底座研读 + 个人增量

BNU「智思体（GEST）」智能教育平台。平台底座为 **Dify 1.17.0**（含官方 `dify-agent` agent server）二次开发部署；本仓库收录两件事：

1. **平台底座的架构研读**（`docs/architecture/`）——能讲清楚系统，才配得上在它上面做的增量
2. **本人设计/实现的增量**——运行时补丁、四个服务模块、智能体设计、技能库策展

## 平台底座 vs 个人增量

```
底座（官方，已研读）                          个人增量（本仓库主体）
────────────────────────                      ────────────────────────
Dify 主 api（应用/会话/模型/知识库）     →     dify-mods/patches：记忆身份接线、
                                              多模态历史修复、文件 URL 重签
dify-agent server（pydantic-ai 分层、   →     dify-mods/overrides：并发闸门、
  沙箱多后端 + lease、Redis run store)         磁盘配额 2GB、job 上限、复读熔断
沙箱（local/e2b + SSRF 代理）           →     沙箱内 pip 内网源、资源治理链
云端 CLI 技能市场（2063 个）             →     skills-catalog/：筛选/分类/去重/
                                              描述重写 → 248 精选技能库
```

底座架构研读笔记：[`docs/architecture/dify-agent-runtime.md`](docs/architecture/dify-agent-runtime.md)

## 个人增量模块

| 模块 | 内容 | 规模 | 协议/端口 |
|---|---|---|---|
| `dify-mods/` | 对官方运行时的二次开发：9 个补丁（+545 行，patch 形式）+ 2 个资源治理覆盖文件 | ~700 行增量 | — |
| `usermem/` | 跨会话用户记忆服务（MCP + REST 双协议） | ~1200 行 Node.js | 8083 |
| `ppt-judge/` | PPT 评审 MCP（LibreOffice 转换 + LLM 评审 + 标注图） | ~1000 行 Python | 8003 |
| `fogsight/` | 互动教学动画生成 MCP 封装层（引擎为开源改造，见归属声明） | 封装层 ~700 行 | 8001 |
| `agents/` | 两个自设计智能体的完整配置与 31 版迭代快照 | 38 个文件 | — |
| `skills-catalog/` | 教育场景技能库策展管线：2063 → 248，16 一级分类，7 轮去重，可回滚 | 元数据 + 管线脚本 | — |

另有 `docs/deploy/`（MIGRATION / DEPLOY-STATUS）：MCP 集群从旧服务器迁移到华为鲲鹏 ARM 内网机的真实运维记录。

## 架构位置

```
Web 前端（gest 前端机）
   │
Dify 主 api（底座）── 个人补丁：记忆身份注入 / 多模态历史 / 文件 URL
   │  POST /runs + SSE
dify-agent server（底座）── 个人补丁：复读熔断 / run_failed 兜底 / 文件通道
   │  并发闸门（每用户 2 run，429）
   ├── shell layer ── 个人补丁：磁盘配额 2GB + job 上限 32
   ├── 沙箱（local / e2b + SSRF 代理）
   └── MCP 工具集（28 个）
       ├── usermem        跨会话记忆：按用户隔离 + cron 主动任务
       ├── ppt-judge      PPT 评审链路
       ├── fogsight-mcp   动画生成（含 TTS）
       └── 图表 / 检索 / 定时任务 / 交付物归档 …
```

## 归属与出处声明

- Dify 主 api 与 `dify-agent/` 为 Dify 官方 Apache-2.0 代码，**不是本人编写**；本人增量为 `dify-mods/` 中的 patch 与 override（均明确标注 upstream 基线）。
- `fogsight/app.py`、`app_3d.py` 等引擎文件来自开源项目 **Fogsight.ai**（LICENSE：CC BY-NC-ND 4.0，随附于 `fogsight/LICENSE`）；本人贡献为 MCP 封装层与平台对接脚本。仓库私有、不分发，保留原 LICENSE 与出处。
- 智能体配置快照导出自平台数据库快照表，为本人迭代过程的真实记录。

## 安全与隐私

- 真实凭据与用户数据不入库：`.env`（含 USERMEM_KEY）、`usermem/data/`（真实学生数据）、`fogsight/credentials.json` 均已排除，各服务提供 `.env.example`。
- 历史快照中的旧企业微信 webhook 密钥已脱敏为 `<REDACTED>`。
- 内网 IP（RFC1918）保留以还原部署上下文，无公网暴露面。

## 运行

各模块目录内有独立说明与 `.env.example`；MCP 服务均可以 Streamable HTTP 接入 Dify「自定义 MCP 工具」。
