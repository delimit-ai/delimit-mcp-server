"""
Bridge to delimit-os MCP server.
Tier 2 Platform tools — pass-through to the OS orchestration layer.

These do NOT re-implement OS logic. They translate requests
and forward to the running delimit-os server via direct import.
"""

import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.os_bridge")

OS_PACKAGE = Path("/home/delimit/.delimit_suite/packages/delimit-os")


def _ensure_os_path():
    if str(OS_PACKAGE) not in sys.path:
        sys.path.insert(0, str(OS_PACKAGE))


def create_plan(operation: str, target: str, parameters: Optional[Dict] = None, require_approval: bool = True) -> Dict[str, Any]:
    """Create an execution plan via delimit-os."""
    _ensure_os_path()
    try:
        from server import PLANS
        import uuid, time

        plan_id = f"PLAN-{str(uuid.uuid4())[:8].upper()}"
        risk_level = "LOW"
        if any(x in operation.lower() for x in ["prod", "delete", "drop", "rm"]):
            risk_level = "HIGH"
        elif any(x in operation.lower() for x in ["deploy", "restart", "update"]):
            risk_level = "MEDIUM"

        plan = {
            "plan_id": plan_id,
            "operation": operation,
            "target": target,
            "parameters": parameters or {},
            "risk_level": risk_level,
            "status": "PENDING_APPROVAL" if require_approval else "READY",
            "created_at": time.time(),
        }
        PLANS[plan_id] = plan
        return plan
    except ImportError:
        return {"error": "delimit-os not available", "fallback": True}


def get_status() -> Dict[str, Any]:
    """Get current OS status."""
    _ensure_os_path()
    try:
        from server import PLANS, TASKS, TOKENS
        return {
            "status": "operational",
            "plans": len(PLANS),
            "tasks": len(TASKS),
            "tokens": len(TOKENS),
        }
    except ImportError:
        return {"status": "unavailable", "error": "delimit-os not loaded"}


def check_gates(plan_id: str) -> Dict[str, Any]:
    """Check governance gates for a plan."""
    _ensure_os_path()
    try:
        from server import PLANS
        plan = PLANS.get(plan_id)
        if not plan:
            return {"error": f"Plan {plan_id} not found"}
        return {
            "plan_id": plan_id,
            "gates_passed": plan.get("status") in ("READY", "APPROVED"),
            "status": plan.get("status"),
        }
    except ImportError:
        return {"error": "delimit-os not available"}
