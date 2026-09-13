#!/usr/bin/env python3
"""Synthetic MCP backend: no production imports, credentials, or network access."""

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


mode = sys.argv[1]
audit_path = Path(sys.argv[2])
server = FastMCP("synthetic-delimit-backend")


def record(event, **fields):
    with audit_path.open("a", encoding="utf-8") as audit:
        audit.write(json.dumps({"event": event, **fields}) + "\n")


record("started")


@server.tool()
def forbidden_write_tool(venture: str) -> dict:
    """A synthetic tool the adapter must never advertise or invoke."""
    record("forbidden_called", venture=venture)
    return {"unexpected": True}


if mode != "missing":
    @server.tool()
    def delimit_ledger_context(venture: str) -> dict:
        """Read a synthetic handoff; this function has no real ledger access."""
        record("called", venture=venture)
        if mode == "error":
            raise RuntimeError("BACKEND_PRIVATE_ERROR_SENTINEL")
        return {
            "synthetic": True,
            "venture": venture,
            "open_item": "SYN-E",
            "completed_item_to_preserve": "SYN-D",
        }


server.run(transport="stdio")
