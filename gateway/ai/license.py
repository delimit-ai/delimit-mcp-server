"""
Delimit license — thin shim.
The enforcement logic is in license_core (shipped as compiled binary).
This shim handles imports and provides fallback error messages.
"""
from pathlib import Path

# Always export LICENSE_FILE for tests and external access
LICENSE_FILE = Path.home() / ".delimit" / "license.json"

try:
    from ai.license_core import (
        load_license as get_license,
        check_premium as is_premium,
        gate_tool as require_premium,
        activate as activate_license,
        PRO_TOOLS,
        FREE_TRIAL_LIMITS,
    )
except ImportError:
    # license_core not available (development mode or missing binary)
    import json
    import time
    from pathlib import Path

    LICENSE_FILE = Path.home() / ".delimit" / "license.json"

    PRO_TOOLS = frozenset({
        "delimit_gov_evaluate", "delimit_gov_policy", "delimit_gov_run", "delimit_gov_verify",
        "delimit_os_plan", "delimit_os_status", "delimit_os_gates",
        "delimit_deploy_plan", "delimit_deploy_build", "delimit_deploy_publish",
        "delimit_deploy_verify", "delimit_deploy_rollback", "delimit_deploy_status",
        "delimit_deploy_site", "delimit_deploy_npm",
        "delimit_memory_store", "delimit_memory_search", "delimit_memory_recent",
        "delimit_vault_search", "delimit_vault_snapshot", "delimit_vault_health",
        "delimit_evidence_collect", "delimit_evidence_verify",
        "delimit_deliberate", "delimit_models",
        "delimit_obs_metrics", "delimit_obs_logs", "delimit_obs_status",
        "delimit_release_plan", "delimit_release_status", "delimit_release_sync",
        "delimit_cost_analyze", "delimit_cost_optimize", "delimit_cost_alert",
    })
    FREE_TRIAL_LIMITS = {"delimit_deliberate": 3}

    def get_license() -> dict:
        if not LICENSE_FILE.exists():
            return {"tier": "free", "valid": True}
        try:
            return json.loads(LICENSE_FILE.read_text())
        except Exception:
            return {"tier": "free", "valid": True}

    def is_premium() -> bool:
        lic = get_license()
        return lic.get("tier") in ("pro", "enterprise") and lic.get("valid", False)

    def require_premium(tool_name: str) -> dict | None:
        full_name = tool_name if tool_name.startswith("delimit_") else f"delimit_{tool_name}"
        if full_name not in PRO_TOOLS:
            return None
        if is_premium():
            return None
        return {
            "error": f"'{tool_name}' requires Delimit Pro. Upgrade at https://delimit.ai/pricing",
            "status": "premium_required",
            "tool": tool_name,
            "current_tier": get_license().get("tier", "free"),
        }

    def activate_license(key: str) -> dict:
        return {"error": "License core not available. Reinstall: npx delimit-cli setup"}
