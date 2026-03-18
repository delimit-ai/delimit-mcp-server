"""
Bridge to delimit-vault package.
Tier 2 Platform tools — artifact and credential storage.
"""

import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.vault_bridge")

VAULT_PACKAGE = Path("/home/delimit/.delimit_suite/packages/delimit-vault")

_server = None


def _get_server():
    """Return the vault server instance (lazy — no async init here).

    The Qdrant client uses aiohttp which is bound to the event loop it was
    created on.  ``run_async`` creates a *new* loop per call, so we must NOT
    call ``_initialize_clients()`` here.  Instead each bridge method calls
    ``_ensure_initialized()`` inside the *same* ``run_async`` invocation that
    performs the actual operation, keeping the aiohttp session alive for the
    duration of the request.
    """
    global _server
    if _server is not None:
        return _server
    pkg_path = str(VAULT_PACKAGE / "delimit_vault_mcp")
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)
    if str(VAULT_PACKAGE) not in sys.path:
        sys.path.insert(0, str(VAULT_PACKAGE))
    try:
        from delimit_vault_mcp.server import DelimitVaultServer
        _server = DelimitVaultServer()
        return _server
    except Exception as e:
        logger.warning(f"Failed to init vault server: {e}")
        return None


async def _ensure_initialized(srv):
    """(Re-)initialize Qdrant client on the *current* event loop.

    Because ``run_async`` may create a fresh event loop for each bridge call,
    the previous aiohttp session becomes invalid.  We unconditionally
    re-initialize to bind the session to the current loop.
    """
    await srv._initialize_clients()


def _extract_text(result) -> Dict[str, Any]:
    """Extract text from MCP TextContent objects or raw results."""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"result": result}
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        # Handle list of TextContent objects
        texts = []
        for item in result:
            if hasattr(item, "text"):
                try:
                    texts.append(json.loads(item.text))
                except (json.JSONDecodeError, TypeError):
                    texts.append({"text": str(item.text)})
            else:
                texts.append(str(item))
        return texts[0] if len(texts) == 1 else {"results": texts}
    if hasattr(result, "text"):
        try:
            return json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return {"text": str(result.text)}
    return {"result": str(result)}


def search(query: str) -> Dict[str, Any]:
    """Search vault entries."""
    srv = _get_server()
    if srv is None:
        return {"error": "Vault server unavailable", "results": []}
    try:
        async def _do():
            await _ensure_initialized(srv)
            return await srv._handle_search({"query": query})
        result = run_async(_do())
        return _extract_text(result)
    except Exception as e:
        return {"error": f"Vault search failed: {e}", "results": []}


def health() -> Dict[str, Any]:
    """Check vault health."""
    srv = _get_server()
    if srv is None:
        return {"status": "unavailable", "error": "Vault server not initialized"}
    try:
        async def _do():
            await _ensure_initialized(srv)
            return await srv._handle_health()
        result = run_async(_do())
        return _extract_text(result)
    except Exception as e:
        return {"status": "unavailable", "error": str(e)}


def snapshot(task_id: str = "vault-snapshot") -> Dict[str, Any]:
    """Get vault snapshot."""
    srv = _get_server()
    if srv is None:
        return {"error": "Vault server unavailable"}
    try:
        async def _do():
            await _ensure_initialized(srv)
            return await srv._handle_snapshot({"task_id": task_id})
        result = run_async(_do())
        return _extract_text(result)
    except Exception as e:
        return {"error": f"Vault snapshot failed: {e}"}
