"""Base worker class — defines the bounded capability surface.

Every worker:
  - Receives a ledger item as input
  - Has access to READ-ONLY tools only (lint, diff, grep, read)
  - Produces a WorkerResult containing a work-order artifact
  - Cannot call state-changing tools (write, commit, push, notify)
  - Records its work in the audit trail

The capability boundary is enforced by the ALLOWED_TOOLS whitelist.
Workers that try to call tools outside the whitelist get an error,
not a silent fallback.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.workers")

# Read-only tool whitelist — workers CANNOT call anything else.
# This is the "sandboxed" property from the swarm charter.
ALLOWED_TOOLS = frozenset({
    "delimit_lint",
    "delimit_diff",
    "delimit_semver",
    "delimit_spec_health",
    "delimit_repo_analyze",
    "delimit_sense",
    "delimit_ledger_query",
    "delimit_ledger_context",
    "delimit_memory_search",
    "delimit_memory_recent",
    "delimit_intel_query",
    "delimit_gov_health",
    # File system reads (not MCP tools, used via subprocess)
    "grep",
    "read_file",
    "glob",
})

# Explicitly DENIED tools — existence check, not exhaustive.
# Workers hitting these get a clear error message.
DENIED_TOOLS = frozenset({
    "delimit_ledger_add",
    "delimit_ledger_update",
    "delimit_ledger_done",
    "delimit_memory_store",
    "delimit_notify",
    "delimit_social_post",
    "delimit_deploy_publish",
    "delimit_deploy_site",
    "delimit_secret_store",
    "write_file",
    "edit_file",
    "bash",
})

AUDIT_DIR = Path.home() / ".delimit" / "workers" / "audit"


@dataclass
class WorkerResult:
    """The output of a worker execution."""
    worker_type: str
    ledger_item_id: str
    success: bool
    artifact_path: str = ""
    artifact_preview: str = ""
    work_order_id: str = ""
    error: str = ""
    tools_called: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Worker(ABC):
    """Base class for all read-only workers."""

    worker_type: str = "base"
    description: str = "Base worker"

    def __init__(self):
        self._tools_called: List[str] = []

    def check_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is in the worker's allowed set."""
        if tool_name in DENIED_TOOLS:
            logger.warning("Worker %s attempted denied tool: %s", self.worker_type, tool_name)
            return False
        return tool_name in ALLOWED_TOOLS

    def call_tool(self, tool_name: str, **kwargs) -> Any:
        """Call a tool through the bounded surface. Raises if denied."""
        if not self.check_tool_allowed(tool_name):
            raise PermissionError(
                f"Worker '{self.worker_type}' cannot call '{tool_name}'. "
                f"Allowed: {sorted(ALLOWED_TOOLS)}"
            )
        self._tools_called.append(tool_name)
        # Import and call the tool from the server module
        from ai import server as srv
        fn = getattr(srv, f"delimit_{tool_name}" if not tool_name.startswith("delimit_") else tool_name, None)
        if fn is None:
            raise ValueError(f"Tool '{tool_name}' not found in server module")
        return fn(**kwargs)

    @abstractmethod
    def execute(self, ledger_item: Dict[str, Any]) -> WorkerResult:
        """Execute the worker's task on a ledger item.

        Must return a WorkerResult with an artifact (work order).
        Must NOT modify any state — output only.
        """
        ...

    def run(self, ledger_item: Dict[str, Any]) -> WorkerResult:
        """Run the worker with timing + audit trail."""
        start = time.time()
        try:
            result = self.execute(ledger_item)
        except Exception as e:
            result = WorkerResult(
                worker_type=self.worker_type,
                ledger_item_id=ledger_item.get("id", "?"),
                success=False,
                error=str(e),
            )
        result.duration_seconds = round(time.time() - start, 2)
        result.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        result.tools_called = self._tools_called.copy()
        self._tools_called.clear()

        # Audit trail
        self._record_audit(result)
        return result

    def _record_audit(self, result: WorkerResult):
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        audit_file = AUDIT_DIR / f"{self.worker_type}.jsonl"
        try:
            with audit_file.open("a") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except Exception as e:
            logger.warning("Failed to write worker audit: %s", e)
