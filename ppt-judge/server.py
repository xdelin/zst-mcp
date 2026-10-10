from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
# --- CACHE PATCH INJECTED ---
import hashlib
import asyncio
from functools import wraps
import json
import inspect

TOOL_CACHE = {}

def cache_mcp_request(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        
        keys_dict = dict(bound.arguments)
        key_str = json.dumps(keys_dict, sort_keys=True)
        cache_key = hashlib.md5(f"{func.__name__}:{key_str}".encode('utf-8')).hexdigest()
        
        logger.info(f"Checking cache for {func.__name__}, key: {cache_key}")
        
        if cache_key in TOOL_CACHE:
            entry = TOOL_CACHE[cache_key]
            if isinstance(entry, asyncio.Future):
                logger.info(f"Cache hit (Pending): {func.__name__} is still generating. Waiting via shield...")
                return await asyncio.shield(entry)
            else:
                logger.info(f"Cache hit (Done): Returning instantly for {func.__name__}")
                return entry
                
        logger.info(f"Cache miss for {func.__name__}, initializing background generation...")
        fut = asyncio.Future()
        TOOL_CACHE[cache_key] = fut
        
        async def _do_work():
            try:
                res = await func(*args, **kwargs)
                fut.set_result(res)
                TOOL_CACHE[cache_key] = res
            except Exception as e:
                err = f"Error during cached background task: {e}"
                logger.error(err, exc_info=True)
                fut.set_result(err)
                TOOL_CACHE[cache_key] = err

        asyncio.create_task(_do_work())
        
        return await asyncio.shield(fut)
        
    return wrapper
# --- END CACHE PATCH ---


import httpx
import asyncio
import os
import base64
import json
import re
import uuid
import io
import fitz  # PyMuPDF
import tempfile
import logging
from pptx import Presentation
from typing import List, Dict, Any, Optional

# 配置日志
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
    
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "mcp_server.log"),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# 初始化 MCP 服务器
mcp = FastMCP("PPT Evaluation Service")

# 后端 API 地址 (默认本地) - 用于生成外部访问链接
API_BASE = os.getenv("API_BASE_URL", "http://<old-server-ip>")
# 内部 API 地址 (用于 Docker 容器间通信) - 默认为 API_BASE
INTERNAL_API_BASE = os.getenv("INTERNAL_API_BASE_URL", "http://127.0.0.1:6801")
# Dify 平台地址 - 用于补全 Dify 文件相对路径
DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "http://<internal-host>")

# 报告保存目录 (wpss-backend/main_api/reports，由 6801 服务静态托管)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "wpss-backend", "main_api", "reports"))

# --- RATE LIMIT PATCH ---
# 全局 + 租户两级限流，额度按 PPT 页数加权（后端全页并发分析，负载随页数线性增长）
from rate_limit import WeightedRateLimiter, RateLimitRejected, env_int

# 阶段一（请求闸门，权重恒为 1）：限制同时处于「下载 + 解析/转换 PPT」阶段的请求数，
# 保护 COM / LibreOffice 这类重资源转换不被并发打爆
REQUEST_GATE = WeightedRateLimiter(
    global_budget=env_int("PPT_RATE_GLOBAL_REQUESTS", 8),
    tenant_budget=env_int("PPT_RATE_TENANT_REQUESTS", 3),
    max_waiting_per_tenant=env_int("PPT_RATE_MAX_WAITING", 4),
    wait_timeout=env_int("PPT_RATE_WAIT_TIMEOUT", 120),
)
# 阶段二（页数加权额度）：调用后端 /ai/chat 前获取，额度 = PPT 页数
PAGE_LIMITER = WeightedRateLimiter(
    global_budget=env_int("PPT_RATE_GLOBAL_PAGES", 50),
    tenant_budget=env_int("PPT_RATE_TENANT_PAGES", 20),
    max_waiting_per_tenant=env_int("PPT_RATE_MAX_WAITING", 4),
    wait_timeout=env_int("PPT_RATE_WAIT_TIMEOUT", 120),
)


def get_tenant_key() -> str:
    """租户标识：MCP session id > 客户端 IP > default（REST 直调无 MCP 上下文，共享 default 桶）"""
    try:
        ctx = mcp.get_context()
        sid = getattr(ctx, "session_id", None)
        if sid:
            return f"sess:{sid}"
        req = getattr(getattr(ctx, "request_context", None), "request", None)
        if req is not None:
            qp = getattr(req, "query_params", None)
            qs = qp.get("session_id") if qp else None
            if qs:
                return f"sess:{qs}"
            host = getattr(getattr(req, "client", None), "host", "") or ""
            if host:
                return f"ip:{host}"
    except Exception:
        pass
    return "default"


def rate_limited(func):
    """请求闸门装饰器：置于 cache_mcp_request 之内——相同请求先单飞合并，再参与限流。"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        tenant = get_tenant_key()
        try:
            async with REQUEST_GATE.slot(tenant):
                return await func(*args, **kwargs)
        except RateLimitRejected as e:
            logger.warning(f"Request gate rejected tenant={tenant}: {e.message}")
            return f"Error: {e.message}"
    return wrapper
# --- END RATE LIMIT PATCH ---

def save_report(content: str) -> str:
    """保存报告为 Markdown 和 HTML，返回下载链接"""
    import markdown

    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)

    base_url = API_BASE.rstrip('/')
    content_fixed = content.replace('](/api/static/', f']({base_url}/api/static/')
    content_fixed = content_fixed.replace('src="/api/static/', f'src="{base_url}/api/static/')
    # Also replace any localhost references from backend
    content_fixed = content_fixed.replace('http://localhost:6801', base_url)
    # 将 "### 第 X 页" 提升为 h2，与 h3 问题标题拉开层级
    import re
    content_fixed = re.sub(r'^\s*### (第 \d+ 页)', r'## \1', content_fixed, flags=re.MULTILINE)

    report_id = uuid.uuid4().hex
    md_filename = f"report_{report_id}.md"
    html_filename = f"report_{report_id}.html"

    # 保存 Markdown
    with open(os.path.join(REPORT_DIR, md_filename), "w", encoding="utf-8") as f:
        f.write(content_fixed)

    # 用 markdown 库将 Markdown 渲染为 HTML
    rendered_html = markdown.markdown(
        content_fixed,
        extensions=['tables', 'fenced_code', 'nl2br', 'sane_lists']
    )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PPT Analysis Report</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", sans-serif; max-width: 960px; margin: 0 auto; padding: 24px; line-height: 1.8; color: #1a1a2e; background: #f8f9fa; }}
        .report-container {{ background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
        h1 {{ font-size: 1.8em; color: #1a1a2e; border-bottom: 3px solid #4361ee; padding-bottom: 12px; margin-bottom: 8px; }}
        .tip {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
        h2 {{ font-size: 1.4em; color: #2d3436; margin-top: 28px; margin-bottom: 12px; padding-left: 12px; border-left: 4px solid #4361ee; }}
        h3 {{ font-size: 1.25em; color: #1a1a2e; font-weight: 700; margin-top: 20px; margin-bottom: 8px; }}
        h4 {{ font-size: 1em; color: #636e72; margin-top: 16px; margin-bottom: 6px; }}
        p {{ margin-bottom: 10px; color: #2d3436; }}
        ul, ol {{ margin-left: 24px; margin-bottom: 12px; }}
        li {{ margin-bottom: 6px; }}
        strong {{ color: #d63031; }}
        img {{ max-width: 100%; height: auto; border-radius: 8px; border: 1px solid #e5e7eb; margin: 12px 0; display: block; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; color: #e84393; }}
        pre {{ background: #2d3436; color: #dfe6e9; padding: 16px; border-radius: 8px; overflow-x: auto; margin: 12px 0; }}
        pre code {{ background: none; color: inherit; padding: 0; }}
        blockquote {{ border-left: 4px solid #4361ee; padding: 8px 16px; margin: 12px 0; background: #f0f4ff; border-radius: 0 8px 8px 0; color: #555; }}
        hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 24px 0; }}
        table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
        th, td {{ border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; }}
        th {{ background: #f0f4ff; font-weight: 600; }}
        a {{ color: #4361ee; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="report-container">
        <h1>PPT Analysis Report</h1>
        <div class="tip">PPT 分析报告 — 由 AI 视觉模型生成</div>
        {rendered_html}
    </div>
</body>
</html>"""
    with open(os.path.join(REPORT_DIR, html_filename), "w", encoding="utf-8") as f:
        f.write(html_content)

    return f"{base_url}/reports/{html_filename}"

TEMP_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "mcp_uploads")
if not os.path.exists(TEMP_DOWNLOAD_DIR):
    os.makedirs(TEMP_DOWNLOAD_DIR)

async def download_to_temp(file_input: str, label: str = "file") -> str:
    """If file_input is a URL (http/https) or Dify relative path, download to temp dir and return local path.
       If it's already a local path, return as-is."""
    # Auto-detect Dify relative paths like /files/xxx/file-preview?...
    if file_input.startswith("/files/"):
        file_input = DIFY_BASE_URL.rstrip("/") + file_input
        logger.info(f"Auto-prepended Dify base URL: {file_input[:120]}...")
    # Dify /file-preview 路由只有带 as_attachment=true 才返回 Content-Disposition
    # 文件名；模型转述注入 URL 时偶尔会把末尾该参数丢掉，这里强制补上
    if "/file-preview" in file_input and "as_attachment" not in file_input:
        file_input += ("&" if "?" in file_input else "?") + "as_attachment=true"
        logger.info("Appended as_attachment=true to file-preview URL")
    if file_input.startswith(("http://", "https://")):
        logger.info(f"Downloading {label} from URL: {file_input[:120]}...")
        async with httpx.AsyncClient(timeout=1200.0, follow_redirects=True, trust_env=False, verify=False) as client:
            resp = await client.get(file_input)
            resp.raise_for_status()
            # Try to get filename from Content-Disposition or URL
            filename = None
            cd = resp.headers.get("content-disposition", "")
            # RFC 5987: filename*=UTF-8''<percent-encoded name>（Dify /file-preview
            # 带 as_attachment=true 时返回这种格式，URL 路径本身无扩展名，必须靠它还原文件名）
            import re
            m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.IGNORECASE)
            if m:
                from urllib.parse import unquote
                filename = unquote(m.group(1).strip().strip('"\''))
            if not filename and "filename=" in cd:
                filename = cd.split("filename=")[-1].strip(' "\'')
            if not filename:
                from urllib.parse import urlparse, unquote
                path = urlparse(file_input).path
                filename = unquote(os.path.basename(path)) if path else None
            if not filename or '.' not in filename:
                # fallback: guess extension from content-type
                ct = resp.headers.get("content-type", "")
                ext_map = {"application/pdf": ".pdf", "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
                           "image/png": ".png", "image/jpeg": ".jpg", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
                ext = ext_map.get(ct.split(";")[0].strip(), ".bin")
                if ext == ".bin":
                    # 兜底：按文件头识别常见格式，避免 content-type 是
                    # octet-stream 时被误判成 .bin 拒绝解析
                    head = resp.content[:8192]
                    if head.startswith(b"PK\x03\x04"):
                        if b"ppt/" in head or b"presentation" in head:
                            ext = ".pptx"
                        elif b"word/" in head:
                            ext = ".docx"
                    elif head.startswith(b"%PDF"):
                        ext = ".pdf"
                filename = f"{label}_{uuid.uuid4().hex[:8]}{ext}"
            os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
            local_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{filename}")
            with open(local_path, "wb") as f:
                f.write(resp.content)
            logger.info(f"Downloaded {label} to {local_path} ({len(resp.content)} bytes)")
            return local_path
    else:
        return file_input


def process_pptx(file_path: str) -> tuple[List[Dict], List[str]]:

    """处理 PPTX：提取文本结构 和 转换为图片(Base64) - 使用 COM"""
    logger.info(f"Processing PPTX: {file_path}")
    
    # 确保路径是绝对路径
    abs_path = os.path.abspath(file_path)
    screenshots = []
    slides_data = []
    presentation = None
    ppt_app = None
    
    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        try:
            ppt_app = win32com.client.GetActiveObject("PowerPoint.Application")
        except:
            ppt_app = win32com.client.Dispatch("PowerPoint.Application")
            
        # WithWindow=False 避免弹出窗口
        presentation = ppt_app.Presentations.Open(abs_path, WithWindow=False, ReadOnly=True)
        
        for slide in presentation.Slides:
            # 1. 提取文本
            text_content = []
            for shape in slide.Shapes:
                if shape.HasTextFrame:
                    if shape.TextFrame.HasText:
                        text_content.append(shape.TextFrame.TextRange.Text)
            slides_data.append({"elements": [{"content": "\n".join(text_content)}]})

            # 2. 截图
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            
            # Export(FileName, FilterName, ScaleWidth, ScaleHeight)
            slide.Export(tmp_path, "JPG", 1280, 720)
            
            with open(tmp_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode('utf-8')
                screenshots.append(f"data:image/jpeg;base64,{encoded}")
            
            try:
                os.unlink(tmp_path)
            except:
                pass
        logger.info(f"Successfully processed PPTX. Slides: {len(slides_data)}, Screenshots: {len(screenshots)}")
                
    except Exception as e:
        # 如果 COM 失败，尝试降级使用 python-pptx 仅提取文本
        logger.error(f"COM Error processing PPTX: {e}. Fallback to python-pptx.")
        print(f"COM Error processing PPTX: {e}. Fallback to python-pptx.")
        try:
            prs = Presentation(file_path)
            slides_data = []
            for slide in prs.slides:
                slide_text = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text.append(shape.text.strip())
                slides_data.append({"elements": [{"content": "\n".join(slide_text)}]})
            # Linux: 用 LibreOffice 将 PPTX 转 PDF，再用 PyMuPDF 渲染截图
            screenshots = []
            try:
                import subprocess
                with tempfile.TemporaryDirectory() as tmpdir:
                    abs_path = os.path.abspath(file_path)
                    conv_result = subprocess.run(
                        [os.getenv("LIBREOFFICE_BIN", "libreoffice"), f"-env:UserInstallation=file://{os.path.join(tmpdir, 'lo_profile')}", "--headless", "--invisible", "--nologo", "--nodefault", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, abs_path],
                        capture_output=True, timeout=120
                    )
                    pdf_name = os.path.splitext(os.path.basename(abs_path))[0] + ".pdf"
                    pdf_path = os.path.join(tmpdir, pdf_name)
                    if os.path.exists(pdf_path):
                        pdf_doc = fitz.open(pdf_path)
                        for page in pdf_doc:
                            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                            img_bytes = pix.tobytes("png")
                            base64_str = f"data:image/png;base64,{base64.b64encode(img_bytes).decode('utf-8')}"
                            screenshots.append(base64_str)
                        pdf_doc.close()
                        logger.info(f"LibreOffice fallback: extracted {len(screenshots)} screenshots")
                    else:
                        logger.warning(f"LibreOffice conversion failed. stderr: {conv_result.stderr.decode()}")
            except Exception as lo_err:
                logger.warning(f"LibreOffice screenshot extraction failed: {lo_err}")
            return slides_data, screenshots
        except Exception as ex:
             logger.error(f"Fallback failed: {ex}")
             raise ex
    finally:
        if presentation:
            try:
                presentation.Close()
            except:
                pass
    
    return slides_data, screenshots

def process_pdf(file_path: str) -> tuple[List[Dict], List[str]]:
    """处理 PDF：提取文本结构 和 转换为图片(Base64)"""
    logger.info(f"Processing PDF: {file_path}")
    doc = fitz.open(file_path)
    slides_data = []
    screenshots = []
    
    for page in doc:
        # 1. 提取文本
        text = page.get_text()
        slides_data.append({
            "elements": [{"content": text}]
        })
        
        # 2. 转换为图片 (PNG)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x zoom for better quality
        img_bytes = pix.tobytes("png")
        base64_str = f"data:image/png;base64,{base64.b64encode(img_bytes).decode('utf-8')}"
        screenshots.append(base64_str)
        
    return slides_data, screenshots


def _encode_local_image_to_data_url(file_path: str) -> str:
    """将本地图片转成 data URL"""
    ext = os.path.splitext(file_path)[1].lower()
    mime_type = "image/png" if ext == ".png" else "image/jpeg"
    with open(file_path, "rb") as img_file:
        encoded = base64.b64encode(img_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def _extract_text_from_word(file_path: str) -> str:
    """提取 DOCX/DOC 文本（Windows 优先）"""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".docx":
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text).strip()
        except Exception as e:
            logger.error(f"DOCX parse failed: {e}")
            return ""

    if ext == ".doc":
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            doc = word.Documents.Open(os.path.abspath(file_path))
            text = doc.Content.Text.strip()
            doc.Close(False)
            word.Quit()
            return text
        except Exception as e:
            logger.error(f"DOC parse failed: {e}")
            return ""

    return ""


def _extract_evidence(file_path: str) -> tuple[str, List[str]]:
    """
    解析证据文件，返回：(证据文本, 证据图片列表data-url)
    支持：图片 / PDF / DOCX / DOC
    """
    if not file_path or not os.path.exists(file_path):
        return "", []

    ext = os.path.splitext(file_path)[1].lower()
    evidence_text = ""
    evidence_images: List[str] = []

    if ext in [".png", ".jpg", ".jpeg"]:
        evidence_images.append(_encode_local_image_to_data_url(file_path))
        evidence_text = "[证据文件] 用户上传了原始图像作为评价依据，请结合该图像进行判断。"
        return evidence_text, evidence_images

    if ext == ".pdf":
        # PDF 既提取文本，也提取页面截图（限制页数，防止 token 过大）
        try:
            doc = fitz.open(file_path)
            text_chunks = []
            for page in doc:
                page_text = page.get_text() or ""
                if page_text.strip():
                    text_chunks.append(page_text)
            evidence_text = "\n".join(text_chunks).strip()

            # 最多附带前 5 页截图作为证据图像
            for idx, page in enumerate(doc):
                if idx >= 5:
                    break
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img_bytes = pix.tobytes("png")
                b64 = base64.b64encode(img_bytes).decode("utf-8")
                evidence_images.append(f"data:image/png;base64,{b64}")
            doc.close()
        except Exception as e:
            logger.error(f"PDF evidence parse failed: {e}")
        return evidence_text, evidence_images

    if ext in [".docx", ".doc"]:
        evidence_text = _extract_text_from_word(file_path)
        return evidence_text, evidence_images

    return "", []


async def _call_ai_chat_stream(payload: Dict[str, Any]) -> str:
    """调用主服务 /ai/chat 流式接口并聚合返回"""
    api_url = f"{INTERNAL_API_BASE}/ai/chat"
    logger.info(f"Calling backend API(stream): {api_url}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(3000.0, connect=60.0), trust_env=False) as client:
        async with client.stream("POST", api_url, json=payload) as response:
            if response.status_code != 200:
                try:
                    error_detail = await response.aread()
                    error_msg = error_detail.decode("utf-8")
                except Exception:
                    error_msg = "No detail"
                logger.error(f"Backend API Error: {response.status_code} - {error_msg}")
                return f"Error: Backend returned status {response.status_code}. Detail: {error_msg}"

            full_response = ""
            async for chunk in response.aiter_text():
                if not full_response:
                    logger.info("First chunk received from backend")
                if len(full_response) % 10000 < len(chunk):
                    logger.info(f"Received {len(full_response)} bytes so far...")
                full_response += chunk

    return full_response


def _extract_annotated_image_urls(text: str) -> List[str]:
    """提取 wpss 返回正文里的标注图 markdown 链接（.../api/static/ppt-images/xxx-annotated.png）。"""
    return re.findall(r"!\[[^\]]*\]\(([^)]*api/static/ppt-images/[^)]+annotated[^)]*)\)", text)


async def _build_tool_result(text_with_images: str, report_url: str):
    """把 wpss 正文里的视觉问题标注图转成 MCP ImageContent 附件返回。

    2026-09-23 前端（gest <internal-host>）已改造：webapp /files 代理指向 Dify、
    nginx 加了 /files 路由、前端把绝对 URL 改写成同源相对路径，图片经
    MCP ImageContent -> Dify tool_files -> agent 节点 files 输出 -> answer 模板
    {{files}} -> message_end/message_files 通道即可在 https 页面正常渲染。
    正文里保留的图片 markdown 会被前端剥离，这里也一并剥掉避免重复。
    返回 [TextContent, ImageContent...]；图片下载失败时退化为纯文本。"""
    image_urls = _extract_annotated_image_urls(text_with_images)
    if not image_urls:
        return f"{text_with_images}\n\n---\n[⬇️ 下载完整评估报告_HTML版本]({report_url})"

    contents = []
    downloaded = 0
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False, verify=False) as client:
            for url in image_urls[:10]:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    png = r.content
                    if png[:8] != b"\x89PNG\r\n\x1a\n":
                        logger.warning(f"Annotated image is not PNG, skipped: {url}")
                        continue
                    contents.append(
                        ImageContent(
                            type="image",
                            data=base64.b64encode(png).decode(),
                            mimeType="image/png",
                        )
                    )
                    downloaded += 1
                except Exception as e:
                    logger.warning(f"Failed to download annotated image {url}: {e}")
    except Exception as e:
        logger.warning(f"Failed to init httpx client for annotated images: {e}")

    # 剥掉正文中的标注图 markdown 及其标题行（图片已作为附件返回）
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*api/static/ppt-images/[^)]*annotated[^)]*\)", "", text_with_images)
    cleaned = re.sub(r"\**\s*🔍 视觉问题标注图\s*\**：?\s*", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    text = f"{cleaned}\n\n---\n[⬇️ 下载完整评估报告_HTML版本]({report_url})"
    contents.insert(0, TextContent(type="text", text=text))
    logger.info(f"Tool result: text + {downloaded} annotated image(s) as ImageContent")
    return contents


@mcp.tool()
@cache_mcp_request
@rate_limited
async def evaluate_ppt_with_evidence(
    ppt_file_path: str,
    evidence_file_path: Optional[str] = None,
    user_query: str = "请综合截图与参考资料对该PPT进行结构、内容、视觉和专业性评价，并给出可执行改进建议。",
):
    """
    对照参考资料评价 PPT：与 evaluate_file 相同的 PPT 质量评价（结构、内容、逻辑、视觉），并额外结合用户提供的参考资料（评分标准、原始材料、优秀范例）做对比分析。待评价的主文件必须是 PPT 类文件（.pptx/.pdf 幻灯片）。
    何时调用：用户要求"结合参考资料/评分标准/原始材料"评价一份 PPT 时。
    不要调用：没有参考资料的普通 PPT 评价用 evaluate_file；阅读或处理普通文档请用文档类工具（如 read_document）。

    Args:
        ppt_file_path: 待评价 PPT 文件的本地路径/URL（仅支持 .pptx/.pdf）
        evidence_file_path: 可选，参考资料的本地路径/URL（支持.png/.jpg/.jpeg/.pdf/.docx/.doc）
        user_query: 可选，用户自定义评价要求
    """
    logger.info(
        f"Received evaluate_ppt_with_evidence request, ppt={ppt_file_path}, evidence={evidence_file_path}"
    )

    # 支持URL下载
    try:
        ppt_file_path = await download_to_temp(ppt_file_path, "ppt")
    except Exception as e:
        return f"Error downloading PPT file: {str(e)}"

    if not os.path.exists(ppt_file_path):
        return f"Error: PPT file not found at {ppt_file_path}"

    ppt_ext = os.path.splitext(ppt_file_path)[1].lower()
    if ppt_ext not in [".pptx", ".pdf"]:
        return "Error: ppt_file_path only supports .pptx or .pdf"

    # 1) 解析 PPT -> 文本结构 + 所有截图
    try:
        if ppt_ext == ".pptx":
            ppt_data, screenshots = process_pptx(ppt_file_path)
        else:
            ppt_data, screenshots = process_pdf(ppt_file_path)
    except Exception as e:
        logger.error(f"Failed to process PPT file: {e}", exc_info=True)
        return f"Error processing ppt file: {str(e)}"

    if not screenshots:
        logger.warning("No screenshots extracted from PPT/PDF, will still evaluate by text content")

    # 2) 解析证据文件（可选）
    evidence_text = ""
    evidence_images: List[str] = []
    if evidence_file_path:
        try:
            evidence_file_path = await download_to_temp(evidence_file_path, "evidence")
        except Exception as e:
            return f"Error downloading evidence file: {str(e)}"
        if not os.path.exists(evidence_file_path):
            return f"Error: evidence_file_path not found at {evidence_file_path}"
        evidence_text, evidence_images = _extract_evidence(evidence_file_path)

    # 3) 构造请求 —— PPT截图和参考文献通过索引严格区分
    ppt_count = len(screenshots)
    merged_screenshots = screenshots[:]  # PPT截图在前

    extra_note = ""
    if evidence_text:
        clipped = evidence_text[:8000]
        extra_note += f"\n\n[参考文献/资料文本]\n{clipped}"
    if evidence_images:
        # 将参考文献图片追加到末尾，但明确告诉模型索引范围
        merged_screenshots.extend(evidence_images)
        extra_note += f"\n\n【重要：图片索引说明】截图编号 1~{ppt_count} 是PPT页面（需要评价和标注的对象），编号 {ppt_count+1}~{ppt_count+len(evidence_images)} 是参考文献/资料图片（仅作为评价依据，不要对其进行视觉分析或问题标注）。"
    if extra_note:
        extra_note += f"\n\n请严格区分：只对前 {ppt_count} 张PPT截图进行视觉问题分析和标注框绘制，参考文献仅用于对比验证PPT内容的准确性。"

    payload = {
        "message": f"{user_query}{extra_note}",
        "ppt_content": json.dumps(ppt_data, ensure_ascii=False),
        "all_screenshots": merged_screenshots,
        "ppt_page_count": ppt_count,  # 告诉后端只有前N张是PPT截图
        "ignore_ppt_context": False,
    }

    # 4) 调用主服务流式接口（MCP工具层聚合结果）——页数加权限流：额度 = PPT 页数
    try:
        async with PAGE_LIMITER.slot(get_tenant_key(), weight=max(1, len(screenshots))):
            full_response = await _call_ai_chat_stream(payload)
            base_url = API_BASE.rstrip("/")
            full_response_fixed = full_response.replace("](/api/static/", f"]({base_url}/api/static/")
            full_response_fixed = full_response_fixed.replace(
                'src="/api/static/', f'src="{base_url}/api/static/'
            )

            report_url = save_report(full_response_fixed)
            return await _build_tool_result(full_response_fixed, report_url)
    except RateLimitRejected as e:
        logger.warning(f"Page limiter rejected: {e.message}")
        return f"Error: {e.message}"
    except Exception as e:
        logger.error(f"Error in evaluate_ppt_with_evidence: {e}", exc_info=True)
        return f"Error: {str(e)}"

@mcp.tool()
@cache_mcp_request
@rate_limited
async def evaluate_file(file_path: str):
    """
    评价 PPT/幻灯片/课件的质量（结构、内容、逻辑、视觉设计），并给出改进建议。仅处理 PPT 类文件：.pptx 演示文稿、幻灯片 .pdf、PPT 页面截图 .png/.jpg。
    何时调用：用户明确要求"评价/点评/审查/打分"一份 PPT、课件、演示文稿或幻灯片截图时。
    不要调用：阅读、总结、翻译、润色普通文档（论文、Word、txt、代码等）——请改用文档类工具（如 read_document），普通文档交给本工具只会报"Unsupported file format"。

    Args:
        file_path: PPT 文件或 PPT 截图的本地路径/URL（仅支持 .pptx/.pdf/.png/.jpg）
    """
    logger.info(f"Received evaluate_file request for: {file_path}")

    # 支持URL下载
    try:
        file_path = await download_to_temp(file_path, "file")
    except Exception as e:
        return f"Error downloading file: {str(e)}"

    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return f"Error: File not found at {file_path}"
    
    file_ext = os.path.splitext(file_path)[1].lower()
    payload = {}
    
    # === 1. 预处理文件 ===
    try:
        if file_ext == ".pptx":
            # PPTX: 使用 COM 提取文本和截图
            ppt_data, screenshots = process_pptx(file_path)
            payload = {
                "message": "请分析这份PPT，指出设计和逻辑上的问题并给出建议。",
                "ppt_content": json.dumps(ppt_data),
                "all_screenshots": screenshots, # 传递所有页面的截图
                "ignore_ppt_context": False
            }
            
        elif file_ext == ".pdf":
            # PDF: 提取文本，转换为图片 (传递 all_screenshots)
            ppt_data, screenshots = process_pdf(file_path)
            payload = {
                "message": "请分析这份PDF幻灯片，指出设计和逻辑上的问题并给出建议。",
                "ppt_content": json.dumps(ppt_data),
                "all_screenshots": screenshots, # 直接传递所有页面的截图
                "ignore_ppt_context": False
            }

        elif file_ext in [".png", ".jpg", ".jpeg"]:
            # 图片: 直接作为截图进行分析
            with open(file_path, "rb") as img_file:
                encoded_string = base64.b64encode(img_file.read()).decode('utf-8')
                mime_type = "image/png" if file_ext == ".png" else "image/jpeg"
                base64_data = f"data:{mime_type};base64,{encoded_string}"
            
            payload = {
                "message": "请分析这张PPT截图，指出设计和逻辑上的问题并给出建议。",
                "screenshot_base64": base64_data,
                "ppt_content": json.dumps([{"elements": [{"content": "Image Analysis"}]}]),
                "current_slide_index": 0,
                "ignore_ppt_context": False
            }
            
        else:
            logger.error(f"Unsupported file format: {file_ext}")
            return f"Error: Unsupported file format {file_ext}. Supported: .pptx, .pdf, .png, .jpg"
            
    except Exception as e:
        logger.error(f"Error processing file: {e}", exc_info=True)
        return f"Error processing file: {str(e)}"

    # === 2. 调用后端 API ===（页数加权限流：额度 = PPT 页数；截图单张按 1 页计）
    page_weight = len(screenshots) if file_ext in (".pptx", ".pdf") else 1
    try:
        async with PAGE_LIMITER.slot(get_tenant_key(), weight=max(1, page_weight)):
            api_url = f"{INTERNAL_API_BASE}/ai/chat"
            logger.info(f"Calling backend API: {api_url}")

            # trust_env=False 禁用系统代理，防止 localhost 请求走代理导致 502 错误
            async with httpx.AsyncClient(timeout=httpx.Timeout(3000.0, connect=60.0), trust_env=False) as client:
                async with client.stream("POST", api_url, json=payload) as response:
                    if response.status_code != 200:
                        # 尝试读取错误详情
                        try:
                            error_detail = await response.aread()
                            error_msg = error_detail.decode('utf-8')
                        except:
                            error_msg = "No detail"
                        logger.error(f"Backend API Error: {response.status_code} - {error_msg}")
                        return f"Error: Backend returned status {response.status_code}. Detail: {error_msg}"

                    full_response = ""
                    async for chunk in response.aiter_text():
                        if not full_response:
                            logger.info("First chunk received from backend")
                        if len(full_response) % 10000 < len(chunk):
                            logger.info(f"Received {len(full_response)} bytes so far...")
                        full_response += chunk

                logger.info("Successfully received response from backend")

                # 🔥 替换相对路径为绝对路径，确保图片在 Dify/Chat 中正确显示
                base_url = API_BASE.rstrip('/')
                full_response_fixed = full_response.replace('](/api/static/', f']({base_url}/api/static/')
                full_response_fixed = full_response_fixed.replace('src="/api/static/', f'src="{base_url}/api/static/')

                # 保存报告并生成链接
                try:
                    report_url = save_report(full_response) # save_report handles its own replacement logic too
                    logger.info(f"Report saved to: {report_url}")
                    return await _build_tool_result(full_response_fixed, report_url)
                except Exception as e:
                    logger.error(f"Failed to save report: {e}")
                    return f"{full_response_fixed}\n\n(生成报告文件失败: {str(e)})"

    except RateLimitRejected as e:
        logger.warning(f"Page limiter rejected: {e.message}")
        return f"Error: {e.message}"
    except Exception as e:
        logger.error(f"Error communicating with backend API: {e}", exc_info=True)
        return f"Error communicating with backend API: {str(e)}"

if __name__ == "__main__":
    # 运行 MCP 服务器
    mcp.run()
