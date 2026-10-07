# usermem · 跨会话用户记忆服务

为智思体平台智能体提供**跨会话、按用户隔离**的长期记忆能力。同一服务暴露两种协议：

- **MCP**（`/sse`、`/mcp`）：供 Dify 智能体以工具形式调用（28 个绑定工具中的记忆读写部分）
- **REST**（`X-Usermem-Key` 鉴权）：供前端技能面板/管理接口调用

## 结构

| 文件 | 职责 |
|---|---|
| `index.js` | MCP + HTTP 服务入口、鉴权、工具注册 |
| `store.js` | 按用户目录的持久化存储（preferences / skills / tasks / deliverables） |
| `scheduler.js` | cron 调度器：定时/主动任务（如学习提醒），经 Dify API 触发智能体 |
| `catalog.js` | 云端技能目录索引（技能画廊，缓存 5 分钟） |
| `import-cloud-skill.py` | 技能包批量导入工具 |

## 配置

复制 `.env.example` 为 `.env`：`USERMEM_KEY`（REST 鉴权密钥）、`DIFY_BASE_URL`。默认端口 8083。

## 安全

`data/`（真实用户数据）不入库；REST 与 MCP 均需密钥/会话鉴权。
