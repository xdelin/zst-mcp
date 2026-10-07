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
