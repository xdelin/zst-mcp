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


@mcp.tool()
async def evaluate_ppt_with_evidence(
    ppt_file_path: str,
    evidence_file_path: Optional[str] = None,
    user_query: str = "请综合截图与参考资料对该PPT进行结构、内容、视觉和专业性评价，并给出可执行改进建议。",
) -> str:
