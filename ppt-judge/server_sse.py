"""SSE transport + REST API for Dify integration."""
import sys, os, uuid, json, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import mcp, TEMP_DOWNLOAD_DIR, download_to_temp, evaluate_file, evaluate_ppt_with_evidence
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.middleware.cors import CORSMiddleware
import uvicorn
import anyio


async def rest_evaluate(request: Request):
    """Simple REST endpoint: POST /evaluate with JSON body or form-data.
    Accepts: ppt_url (required), evidence_url (optional), user_query (optional)
    Returns: {"report_url": "..."} or {"error": "..."}
    """
    try:
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = dict(form)
        
        ppt_url = data.get("ppt_url") or data.get("ppt_file_path") or ""
        evidence_url = data.get("evidence_url") or data.get("evidence_file_path") or ""
        user_query = data.get("user_query") or ""
        
        if not ppt_url:
            return JSONResponse({"error": "ppt_url is required"}, status_code=400)
        
        if evidence_url:
            result = await evaluate_ppt_with_evidence(
                ppt_file_path=ppt_url,
                evidence_file_path=evidence_url,
                user_query=user_query or "请综合截图与参考资料对该PPT进行结构、内容、视觉和专业性评价，并给出可执行改进建议。"
            )
        else:
            result = await evaluate_file(file_path=ppt_url)
        
        if result.startswith("Error"):
            return JSONResponse({"error": result}, status_code=500)
        
        return JSONResponse({"report_url": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def rest_evaluate_upload(request: Request):
    """Upload file directly (multipart/form-data) to avoid Dify->MCP file URL issues.
    Form fields:
      - ppt_file (required): the PPT/PDF file
      - evidence_file (optional): supporting doc/image
      - user_query (optional): custom evaluation prompt
    Returns: {"report": "..."} or {"error": "..."}
    """
    try:
        form = await request.form()
        ppt_file = form.get("ppt_file")
        evidence_file = form.get("evidence_file")
        user_query = form.get("user_query") or ""

        if ppt_file is None or not hasattr(ppt_file, "filename"):
            return JSONResponse({"error": "ppt_file is required (multipart/form-data)"}, status_code=400)

        os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

        ppt_name = ppt_file.filename or f"upload_{uuid.uuid4().hex[:8]}.pptx"
        ppt_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{ppt_name}")
        with open(ppt_path, "wb") as f:
            f.write(await ppt_file.read())

        evidence_path = ""
        if evidence_file is not None and hasattr(evidence_file, "filename") and evidence_file.filename:
            ev_name = evidence_file.filename
            evidence_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{ev_name}")
            with open(evidence_path, "wb") as f:
                f.write(await evidence_file.read())

        if evidence_path:
            result = await evaluate_ppt_with_evidence(
                ppt_file_path=ppt_path,
                evidence_file_path=evidence_path,
                user_query=user_query or "请综合截图与参考资料对该PPT进行结构、内容、视觉和专业性评价，并给出可执行改进建议。",
            )
        else:
            result = await evaluate_file(file_path=ppt_path)

        if isinstance(result, str) and result.startswith("Error"):
            return JSONResponse({"error": result}, status_code=500)

        return JSONResponse({"report": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def health(request: Request):
    return PlainTextResponse("ok")


if __name__ == "__main__":
    port = int(os.getenv("MCP_SSE_PORT", "8003"))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port

    # Allow external access
    mcp.settings.transport_security.allowed_hosts.append("<internal-host>:*")
    mcp.settings.transport_security.allowed_hosts.append("localhost:127.0.0.1")
    mcp.settings.transport_security.allowed_origins.append("http://<internal-host>:*")

    # Get SSE starlette app
    sse_app = mcp.sse_app()

    # Add REST endpoints
    sse_app.add_route("/evaluate", rest_evaluate, methods=["POST"])
    sse_app.add_route("/evaluate-upload", rest_evaluate_upload, methods=["POST"])
    sse_app.add_route("/health", health, methods=["GET"])
    sse_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    print(f"Starting MCP SSE + REST server on 0.0.0.0:{port}")
    print(f"  SSE:      http://0.0.0.0:{port}/sse")
    print(f"  REST:     POST http://0.0.0.0:{port}/evaluate")
    print(f"  Upload:   POST http://0.0.0.0:{port}/evaluate-upload")
    print(f"  Health:   http://0.0.0.0:{port}/health")

    config = uvicorn.Config(sse_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)
