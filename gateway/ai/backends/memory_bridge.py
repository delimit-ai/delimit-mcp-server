"""
Bridge to delimit-memory package.
Tier 2 Platform tools — semantic memory search and store.
"""

import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.ai.memory_bridge")

MEM_PACKAGE = Path("/home/delimit/.delimit_suite/packages/delimit-memory")

_server = None


def _run_async(coro):
    """Run an async coroutine from sync code, handling nested event loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context (e.g., FastMCP) — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    else:
        return asyncio.run(coro)


def _get_server():
    global _server
    if _server is not None:
        return _server
    pkg_path = str(MEM_PACKAGE / "delimit_memory")
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)
    if str(MEM_PACKAGE) not in sys.path:
        sys.path.insert(0, str(MEM_PACKAGE))
    try:
        from delimit_memory.server import DelimitMemoryServer
        _server = DelimitMemoryServer()
        _run_async(_server._initialize_engine())
        return _server
    except Exception as e:
        logger.warning(f"Failed to init memory server: {e}")
        return None


def search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Semantic search across conversation memory."""
    srv = _get_server()
    if srv is None:
        return {"error": "Memory server unavailable", "results": []}
    try:
        result = _run_async(srv._handle_search({"query": query, "limit": limit}))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"error": f"Memory search failed: {e}", "results": []}


def store(content: str, tags: Optional[list] = None, context: Optional[str] = None) -> Dict[str, Any]:
    """Store a memory entry."""
    srv = _get_server()
    if srv is None:
        return {"error": "Memory server unavailable"}
    try:
        args = {"content": content}
        if tags:
            args["tags"] = tags
        if context:
            args["context"] = context
        result = _run_async(srv._handle_store(args))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"error": f"Memory store failed: {e}"}


def get_recent(limit: int = 5) -> Dict[str, Any]:
    """Get recent work summary."""
    srv = _get_server()
    if srv is None:
        return {"error": "Memory server unavailable", "results": []}
    try:
        result = _run_async(srv._handle_get_recent_work({"limit": limit}))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"error": f"Recent work failed: {e}", "results": []}
