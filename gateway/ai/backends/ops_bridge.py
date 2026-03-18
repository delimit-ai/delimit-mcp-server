"""
Bridge to operational tools: releasepilot, costguard, datasteward, observabilityops.
Governance primitives + internal OS layer.
"""

import sys
import json
import asyncio
import logging
import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.ops_bridge")

PACKAGES = Path("/home/delimit/.delimit_suite/packages")

# Add PACKAGES dir so `from shared.base_server import BaseMCPServer` resolves
_packages = str(PACKAGES)
if _packages not in sys.path:
    sys.path.insert(0, _packages)

_servers = {}


def _call(pkg: str, factory_name: str, method: str, args: Dict, tool_label: str) -> Dict[str, Any]:
    try:
        srv = _servers.get(pkg)
        if srv is None:
            mod = importlib.import_module(f"{pkg}.server")
            factory = getattr(mod, factory_name)
            srv = factory()
            # Disable DSN requirement for observabilityops in bridge context
            if pkg == "observabilityops" and hasattr(srv, "require_dsn_validation"):
                srv.require_dsn_validation = False
            _servers[pkg] = srv
        fn = getattr(srv, method, None)
        if fn is None:
            return {"tool": tool_label, "status": "not_implemented", "error": f"Method {method} not found"}
        result = run_async(fn(args, None))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"tool": tool_label, "error": str(e)}


# ─── ReleasePilot (Governance Primitive) ────────────────────────────────

def release_plan(environment: str, version: str, repository: str, services: Optional[List[str]] = None) -> Dict[str, Any]:
    return _call("releasepilot", "create_releasepilot_server", "_tool_plan",
                 {"environment": environment, "version": version, "repository": repository, "services": services or []}, "release.plan")


def release_validate(environment: str, version: str) -> Dict[str, Any]:
    return _call("releasepilot", "create_releasepilot_server", "_tool_validate",
                 {"environment": environment, "version": version}, "release.validate")


def release_status(environment: str) -> Dict[str, Any]:
    return _call("releasepilot", "create_releasepilot_server", "_tool_status",
                 {"environment": environment}, "release.status")


def release_rollback(environment: str, version: str, to_version: str) -> Dict[str, Any]:
    return _call("releasepilot", "create_releasepilot_server", "_tool_rollback",
                 {"environment": environment, "version": version, "to_version": to_version}, "release.rollback")


def release_history(environment: str, limit: int = 10) -> Dict[str, Any]:
    return _call("releasepilot", "create_releasepilot_server", "_tool_history",
                 {"environment": environment, "limit": limit}, "release.history")


# ─── CostGuard (Governance Primitive) ──────────────────────────────────

def cost_analyze(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    result = _call("costguard", "create_costguard_server", "_tool_analyze",
                   {"target": target, **(options or {})}, "cost.analyze")
    # Guard against hardcoded fake AWS cost data from stub implementation
    if result.get("total_cost") == 1247.83 or result.get("total_cost") == "1247.83":
        return {"tool": "cost.analyze", "status": "not_configured",
                "error": "No cloud provider configured. Cost analyzer returned placeholder data. Set cloud credentials to enable real cost analysis."}
    return result


def cost_optimize(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("costguard", "create_costguard_server", "_tool_optimize",
                 {"target": target, **(options or {})}, "cost.optimize")


def cost_alert(action: str = "list", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("costguard", "create_costguard_server", "_tool_alerts",
                 {"action": action, **(options or {})}, "cost.alert")


# ─── DataSteward (Governance Primitive) ────────────────────────────────

def data_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    result = _call("datasteward", "create_datasteward_server", "_tool_integrity_check",
                   {"database_url": target, **(options or {})}, "data.validate")
    # Guard against stub that returns "passed" with 0 tables checked
    if result.get("tables_checked", -1) == 0 and result.get("integrity_status") == "passed":
        return {"tool": "data.validate", "status": "not_configured",
                "error": "No database configured for validation. Provide a database_url or configure a data source."}
    return result


def data_migrate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("datasteward", "create_datasteward_server", "_tool_migration_status",
                 {"database_url": target, **(options or {})}, "data.migrate")


def data_backup(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("datasteward", "create_datasteward_server", "_tool_backup_plan",
                 {"database_url": target, **(options or {})}, "data.backup")


# ─── ObservabilityOps (Internal OS) ────────────────────────────────────

def obs_metrics(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    return _call("observabilityops", "create_observabilityops_server", "_tool_metrics",
                 {"query": query, "time_range": time_range, "source": source}, "obs.metrics")


def obs_logs(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    return _call("observabilityops", "create_observabilityops_server", "_tool_logs",
                 {"query": query, "time_range": time_range, "source": source}, "obs.logs")


def obs_alerts(action: str, alert_rule: Optional[Dict] = None, rule_id: Optional[str] = None) -> Dict[str, Any]:
    return _call("observabilityops", "create_observabilityops_server", "_tool_alerts",
                 {"action": action, "alert_rule": alert_rule, "rule_id": rule_id}, "obs.alerts")


def obs_status() -> Dict[str, Any]:
    return _call("observabilityops", "create_observabilityops_server", "_tool_status",
                 {}, "obs.status")
