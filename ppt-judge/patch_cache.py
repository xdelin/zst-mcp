import sys

def apply_patch():
    with open('/root/wpss_project/wpss/mcp_server/server.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # Check if patched
    if 'cache_mcp_request' in content:
        print("Already patched")
        return

    import_code = '''
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

'''

    content = content.replace('from mcp.server.fastmcp import FastMCP', 'from mcp.server.fastmcp import FastMCP' + import_code)

    content = content.replace(
        '@mcp.tool()\nasync def evaluate_ppt_with_evidence(',
        '@mcp.tool()\n@cache_mcp_request\nasync def evaluate_ppt_with_evidence('
    )

    content = content.replace(
        '@mcp.tool()\nasync def evaluate_file(',
        '@mcp.tool()\n@cache_mcp_request\nasync def evaluate_file('
    )

    with open('/root/wpss_project/wpss/mcp_server/server.py', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Patched successfully")

apply_patch()
