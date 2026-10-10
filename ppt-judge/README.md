# PPT 评价服务 MCP Server

本项目提供了一个 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 服务器，将本地的 PPT 评价服务暴露为 AI Agent 可调用的工具。

## 功能列表

该 MCP 服务器暴露了以下工具：

1.  `evaluate_file(file_path)`: 评价本地 PPTX 或 PDF 文件。
    *   **PPTX**: 自动提取文本，并调用后端进行截图分析。
    *   **PDF**: 自动将页面转换为图片，并提取文本进行多模态分析。
    *   **输出**: 返回详细的分析报告文本，并附带一个可下载的 Markdown 报告链接。

2.  `evaluate_ppt_with_evidence(ppt_file_path, evidence_file_path?, user_query?)`: 评价 PPT 并结合参考资料。
  *   **主文件**: `ppt_file_path` 支持 `.pptx` / `.pdf`。
  *   **评价方式**: 一次性发送 `all_screenshots`（整份 PPT 全部截图）到大模型，不做逐页逐请求。
  *   **证据文件**: `evidence_file_path` 支持 `.png/.jpg/.jpeg/.pdf/.docx/.doc`。
  *   **输出**: 返回完整分析文本 + 报告下载链接（保持主服务不变，仅新增 MCP 工具能力）。

## 前置条件

1.  确保本地后端服务已启动 (运行根目录下的 `start-all.ps1` 或手动启动 `backend/main_api`)。
    *   默认后端地址: `http://localhost:6801`
2.  安装 Python 3.10+。

## 安装与运行

1.  **安装依赖**:
    ```bash
    pip install -r mcp_server/requirements.txt
    ```

2.  **运行 MCP 服务器**:
    ```bash
    python mcp_server/server.py
    ```
    
    默认情况下，它会使用 stdio (标准输入输出) 模式运行，这是大多数 MCP 客户端 (如 Claude Desktop, Cursor, Dify Local) 的连接方式。

## 在 Dify 中使用

如果您的 Dify 部署支持 MCP (通常通过本地部署或特定的 Agent 配置)：

1.  在 Dify 的工具配置中，选择添加自定义工具或 MCP 工具。
2.  配置连接方式为本地脚本执行 (stdio) 或 SSE (如果修改代码支持 SSE)。
3.  命令路径指向: `python /path/to/your/project/mcp_server/server.py`

## 环境变量

*   `API_BASE_URL`: 后端 API 地址，默认为 `http://localhost:6801`。

## 限流（全局 + 租户）

后端 `/ai/chat` 对整份 PPT 做全页并发视觉分析，负载随页数线性增长，因此限流额度按 **页数加权**（一次请求占用的额度 = PPT 页数），而不是 1 请求 1 份：

*   **全局预算**：整个服务同时在评的页数上限，默认 **50 页**。
*   **租户预算**：单个租户同时在评的页数上限，默认 **20 页**。
*   **请求闸门**：限制同时处于「下载 + 解析/转换」阶段的请求数（全局 8 / 租户 3），保护 COM / LibreOffice 重资源转换。
*   **排队**：单租户最多 4 个请求排队，超过立即拒绝（busy）；排队超过 120s 未拿到额度则拒绝（timeout）；单请求页数超过全局预算立即拒绝（oversize）。
*   **大请求兜底**：单请求页数超过租户预算但不超过全局预算时，若该租户空闲则放行（否则 30 页答辩 PPT 在 20 页预算下永远无法执行）。
*   **租户标识**：MCP session id > 客户端 IP > `default`（REST 直调共享 default 桶）。
*   **去重优先**：限流在 single-flight 缓存去重之后生效，相同请求合并后只占一份额度。

被限流的请求会收到以 `Error:` 开头的友好提示文本，Agent 可直接转述给用户。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PPT_RATE_GLOBAL_PAGES` | `50` | 全局同时在评页数上限 |
| `PPT_RATE_TENANT_PAGES` | `20` | 单租户同时在评页数上限 |
| `PPT_RATE_GLOBAL_REQUESTS` | `8` | 请求闸门：全局并发请求数 |
| `PPT_RATE_TENANT_REQUESTS` | `3` | 请求闸门：单租户并发请求数 |
| `PPT_RATE_MAX_WAITING` | `4` | 单租户最大排队数（超限立即拒绝） |
| `PPT_RATE_WAIT_TIMEOUT` | `120` | 排队等待超时（秒） |

单元测试：`python3 test_rate_limit.py`（或 `pytest test_rate_limit.py`），覆盖全局/租户预算、排队满拒绝、超时拒绝、超大页数拒绝、大请求兜底与状态出清。

## 示例调用

**评价本地 PPTX 文件**:
```json
{
  "name": "evaluate_file",
  "arguments": {
    "file_path": "C:\\Users\\User\\Documents\\presentation.pptx"
  }
}
```

**评价本地 PDF 文件**:
```json
{
  "name": "evaluate_file",
  "arguments": {
    "file_path": "C:\\Users\\User\\Documents\\slides.pdf"
  }
}
```

**评价本地 PPT 并附带证据文件**:
```json
{
  "name": "evaluate_ppt_with_evidence",
  "arguments": {
    "ppt_file_path": "C:\\Users\\User\\Documents\\presentation.pptx",
    "evidence_file_path": "C:\\Users\\User\\Documents\\reference.pdf",
    "user_query": "请重点从结构、学术严谨性、图文一致性三方面评价"
  }
}
```
