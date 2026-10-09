# zst-mcp · 智思体（GEST）教育 Agent 平台 — 个人核心模块（开源版）

BNU「智思体（GEST）」智能教育平台，底座为 **Dify 1.17.0**（含官方 `dify-agent` agent server）。本仓库是从私有主仓库中提取的**本人独立设计/实现的模块**，提交历史为主仓库真实历史的子集（git filter-repo 提取）。

> 提取范围说明：智能体配置快照与技能库策展数据（团队资产）、部署运维文档（含内网拓扑）、动画引擎（许可证限制，见下文归属声明）不在本仓库内。

## 模块总览

| 模块 | 内容 | 规模 |
|---|---|---|
| `dify-mods/` | 对官方 dify-agent 运行时的二次开发：**9 个补丁**（+545 行，patch 形式，标注 upstream 基线）+ **2 个资源治理 override**（并发闸门 429 / 磁盘配额 2GB / job 上限 32 / 复读熔断） | ~700 行增量 |
| `usermem/` | 跨会话用户记忆服务：**单服务双协议**（MCP `/sse`+`/mcp` 供智能体工具调用，REST 供前端技能面板），按用户目录隔离偏好/技能/任务/交付物四类记忆，cron 定时主动任务 | ~1200 行 Node.js |
| `ppt-judge/` | PPT 评审 MCP：rubric 四维度（结构/内容/逻辑/视觉）打分，magic bytes 纠正 content-type 错标，COM / python-pptx / LibreOffice 三级降级解析，MD5 single-flight 缓存合并并发同请求 | ~1000 行 Python |
| `svg-editor/` | 自研 SVG 文本坐标编辑器：针对 LLM 生成动画中文本元素坐标重叠的通病，元素拖改 + 重叠实时高亮 + 包围盒相交检测修复 | ~840 行（全自研） |
| `regress/` | dify-mods 全部补丁的**故障注入回归集**：18 用例覆盖 9 补丁 + 2 override，首轮 ALL GREEN | shell + pytest |
| `docs/architecture/` | dify-agent 底座架构研读笔记（pydantic-ai 分层、沙箱多后端 + lease、Redis run store、SSE 事件流） | — |
| `docs/eval/` | 生产失败数据归因：**966 次团队内测会话 / 失败率 7.2% / 聚类 11 类**，标注每类失败与补丁的对应关系（仅错误元数据统计，对话原文未导出服务器） | — |

## 架构位置

```
Dify 主 api（官方底座）── 个人补丁：记忆身份注入 / 多模态历史修复 / 文件 URL 重签
   │  POST /runs + SSE
dify-agent server（官方底座）── 个人补丁：复读熔断 / 幂等 run_failed / 文件通道
   │  并发闸门（每用户并发 run 上限，超限 429）
   ├── shell layer ── 个人 override：磁盘配额 2GB + 单轮 job 上限 32
   ├── 沙箱（local / e2b + SSRF 代理）
   └── MCP 工具集
       ├── usermem      跨会话记忆（本仓库）
       ├── ppt-judge    PPT 评审（本仓库）
       └── 动画生成 MCP  引擎为 Fogsight.ai（CC BY-NC-ND，不分发；自研 SVG 编辑器已收至 svg-editor/）
```

## 归属与出处声明

- Dify 主 api 与 `dify-agent/` 为 Dify 官方 Apache-2.0 代码，**非本人编写**；本人增量为 `dify-mods/` 中的 patch 与 override（文件内均标注 upstream 基线与改动点）。
- 教学动画引擎来自开源项目 **Fogsight.ai**（CC BY-NC-ND 4.0）：ND 条款禁止分发修改版，故引擎及其封装层不在本仓库；`svg-editor/` 为本人原创代码，独立收录。
- 除上述官方底座与所述引擎外，本仓库其余代码为本人编写，以 MIT License 发布。

## 安全与隐私

- 凭据与用户数据不入库：各服务仅提供 `.env.example`；真实 `.env`、用户数据目录从未入库。
- 发布前已对整个提交历史做脱敏重写：内网 IP、旧服务器公网 IP、实验室域名与用户名均以占位符替换。
- `docs/eval/` 仅含聚合统计口径的错误元数据分析，不含任何对话原文。
