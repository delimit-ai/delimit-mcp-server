"""
Bridge to repo-level tools: repodoctor, configsentry, evidencepack, securitygate.
Tier 3 Extended — repository health, config audit, evidence, security.
"""

import os
import sys
import json
import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.repo_bridge")

PACKAGES = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit"))) / "server" / "packages"

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
            # Some servers need async initialization (e.g. evidencepack)
            init_fn = getattr(srv, "initialize", None)
            if init_fn and asyncio.iscoroutinefunction(init_fn):
                run_async(init_fn())
            _servers[pkg] = srv
        fn = getattr(srv, method, None)
        if fn is None:
            return {"tool": tool_label, "status": "not_implemented", "error": f"Method {method} not found"}
        result = run_async(fn(args, None))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"tool": tool_label, "error": str(e)}


# ─── RepoDoctor ────────────────────────────────────────────────────────

def diagnose(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("repodoctor", "create_repodoctor_server", "_tool_health_check",
                 {"repository_path": target, **(options or {})}, "repo.diagnose")


def analyze(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("repodoctor", "create_repodoctor_server", "_tool_snapshot",
                 {"repository_path": target, **(options or {})}, "repo.analyze")


# ─── ConfigSentry ───────────────────────────────────────────────────────

def config_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("configsentry", "create_configsentry_server", "_tool_validate",
                 {"repository_path": target, **(options or {})}, "config.validate")


def config_audit(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("configsentry", "create_configsentry_server", "_tool_env_audit",
                 {"repository_path": target, **(options or {})}, "config.audit")


# ─── EvidencePack ───────────────────────────────────────────────────────

def evidence_collect(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    result = _call("evidencepack", "create_evidencepack_server", "_tool_list",
                   {"limit": 20, **(options or {})}, "evidence.collect")
    # Provide a clear message when no evidence bundles exist yet
    if isinstance(result, dict) and result.get("total_bundles", -1) == 0:
        result["message"] = (
            "No evidence collected yet. Use evidence.begin to start a collection, "
            "evidence.capture to add items, and evidence.finalize to create a bundle."
        )
    return result


def evidence_verify(bundle_id: Optional[str] = None, bundle_path: Optional[str] = None, options: Optional[Dict] = None) -> Dict[str, Any]:
    args = {**(options or {})}
    if bundle_id:
        args["bundle_id"] = bundle_id
    if bundle_path:
        args["bundle_path"] = bundle_path
    if not bundle_id and not bundle_path:
        return {"tool": "evidence.verify", "status": "no_input", "message": "Provide bundle_id or bundle_path to verify"}
    return _call("evidencepack", "create_evidencepack_server", "_tool_validate",
                 args, "evidence.verify")


# ─── SecurityGate ───────────────────────────────────────────────────────

_INTERNAL_TOKEN = os.environ.get("DELIMIT_INTERNAL_BRIDGE_TOKEN", "delimit-internal-bridge")


def security_scan(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    result = _call("securitygate", "create_securitygate_server", "_tool_scan",
                   {"target": target, "authorization_token": _INTERNAL_TOKEN, **(options or {})}, "security.scan")
    # Guard against fabricated/hardcoded CVE data from stub implementations
    vulns = result.get("vulnerabilities", [])
    if vulns and any("CVE-2023-12345" in str(v.get("id", "")) for v in vulns):
        return {"tool": "security.scan", "status": "not_available",
                "error": "Security scanner returned placeholder data. Install a real vulnerability scanner."}
    return result


def security_audit(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("securitygate", "create_securitygate_server", "_tool_audit",
                 {"target": target, "authorization_token": _INTERNAL_TOKEN, **(options or {})}, "security.audit")
