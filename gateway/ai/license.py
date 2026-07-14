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
        needs_revalidation,
        revalidate_license,
        is_license_valid,
        PRO_TOOLS as _CORE_PRO_TOOLS,
        FREE_TRIAL_LIMITS,
    )
    # Extend compiled PRO_TOOLS with tools added after last binary build.
    # LED-1260: keep this in lockstep with the fallback set below — any tool
    # in the fallback PRO_TOOLS that's NOT in the compiled set must be added
    # here, otherwise customers with the binary get those tools FREE while
    # customers without the binary pay for them (regression-on-success).
    # The runtime test in tests/test_license.py asserts both sets are
    # equal. LED-1410 makes this stronger: the extension set below is
    # CODEGEN from ai/pro_tools.yaml (same SSoT as the compiled
    # set), so the two are equal by construction. The | union with
    # _CORE_PRO_TOOLS is preserved so OLDER compiled .so files that
    # were built before a YAML addition still pick up the new tool
    # at runtime.
    PRO_TOOLS = _CORE_PRO_TOOLS | frozenset({
        # CODEGEN-START: EXTENSION_PRO_TOOLS
    "delimit_audit",
    "delimit_build_loop_daemon",
    "delimit_content_publish",
    "delimit_cost_alert",
    "delimit_cost_analyze",
    "delimit_cost_optimize",
    "delimit_daemon_run",
    "delimit_deliberate",
    "delimit_deploy_build",
    "delimit_deploy_npm",
    "delimit_deploy_plan",
    "delimit_deploy_publish",
    "delimit_deploy_rollback",
    "delimit_deploy_site",
    "delimit_deploy_status",
    "delimit_deploy_verify",
    "delimit_evidence_collect",
    "delimit_evidence_verify",
    "delimit_executor",
    "delimit_github_scan",
    "delimit_gov_evaluate",
    "delimit_gov_new_task",
    "delimit_gov_policy",
    "delimit_gov_run",
    "delimit_gov_verify",
    "delimit_inbox_daemon",
    "delimit_memory_search",
    "delimit_models",
    "delimit_notify_inbox",
    "delimit_obs_logs",
    "delimit_obs_metrics",
    "delimit_obs_status",
    "delimit_os_gates",
    "delimit_os_plan",
    "delimit_os_status",
    "delimit_reddit_scan",
    "delimit_release_plan",
    "delimit_release_status",
    "delimit_release_sync",
    "delimit_screen_record",
    "delimit_screenshot",
    "delimit_security_deliberate",
    "delimit_security_ingest",
    "delimit_social_approve",
    "delimit_social_daemon",
    "delimit_social_generate",
    "delimit_social_history",
    "delimit_social_post",
    "delimit_social_target",
    "delimit_vault_health",
    "delimit_vault_search",
    "delimit_vault_snapshot",
    "delimit_vendor_news_draft",
    "delimit_vendor_news_scan",
        # CODEGEN-END: EXTENSION_PRO_TOOLS
    })
except ImportError:
    # license_core not available — three known cases:
    #   1. Development mode (running from gateway source, no compiled .so)
    #   2. Customer on a non-Linux platform (mac/windows) — first ship is
    #      Linux-only; cross-platform binaries land in a follow-up.
    #   3. Bundle integrity issue (.so missing or corrupt).
    # Fail-closed: do not crash. Fall back to a Python-only implementation
    # so the CLI keeps working; Pro features that depend on the compiled
    # core may be downgraded.
    import sys as _sys
    print(
        "delimit: license_core native module not loadable on this platform; "
        "falling back to Python implementation. Pro features may be downgraded — "
        "contact pro@delimit.ai if you need cross-platform Pro support.",
        file=_sys.stderr,
    )
    import json
    import os
    import time
    from pathlib import Path

    LICENSE_FILE = Path.home() / ".delimit" / "license.json"

    # LED-1410: CODEGEN from ai/pro_tools.yaml — same SSoT as the
    # compiled set above. Memory note preserved here for source readers:
    # delimit_memory_store + delimit_memory_recent are FREE (LED-193).
    # Only delimit_memory_search is Pro.
    PRO_TOOLS = frozenset({
        # CODEGEN-START: FALLBACK_PRO_TOOLS
    "delimit_audit",
    "delimit_build_loop_daemon",
    "delimit_content_publish",
    "delimit_cost_alert",
    "delimit_cost_analyze",
    "delimit_cost_optimize",
    "delimit_daemon_run",
    "delimit_deliberate",
    "delimit_deploy_build",
    "delimit_deploy_npm",
    "delimit_deploy_plan",
    "delimit_deploy_publish",
    "delimit_deploy_rollback",
    "delimit_deploy_site",
    "delimit_deploy_status",
    "delimit_deploy_verify",
    "delimit_evidence_collect",
    "delimit_evidence_verify",
    "delimit_executor",
    "delimit_github_scan",
    "delimit_gov_evaluate",
    "delimit_gov_new_task",
    "delimit_gov_policy",
    "delimit_gov_run",
    "delimit_gov_verify",
    "delimit_inbox_daemon",
    "delimit_memory_search",
    "delimit_models",
    "delimit_notify_inbox",
    "delimit_obs_logs",
    "delimit_obs_metrics",
    "delimit_obs_status",
    "delimit_os_gates",
    "delimit_os_plan",
    "delimit_os_status",
    "delimit_reddit_scan",
    "delimit_release_plan",
    "delimit_release_status",
    "delimit_release_sync",
    "delimit_screen_record",
    "delimit_screenshot",
    "delimit_security_deliberate",
    "delimit_security_ingest",
    "delimit_social_approve",
    "delimit_social_daemon",
    "delimit_social_generate",
    "delimit_social_history",
    "delimit_social_post",
    "delimit_social_target",
    "delimit_vault_health",
    "delimit_vault_search",
    "delimit_vault_snapshot",
    "delimit_vendor_news_draft",
    "delimit_vendor_news_scan",
        # CODEGEN-END: FALLBACK_PRO_TOOLS
    })
    FREE_TRIAL_LIMITS = {"delimit_deliberate": 3}

    REVALIDATION_INTERVAL = 30 * 86400  # 30 days
    GRACE_PERIOD = 7 * 86400
    HARD_BLOCK = 14 * 86400

    def get_license() -> dict:
        if not LICENSE_FILE.exists():
            return {"tier": "free", "valid": True}
        try:
            data = json.loads(LICENSE_FILE.read_text())
            if data.get("expires_at") and data["expires_at"] < time.time():
                return {"tier": "free", "valid": True, "expired": True}
            if data.get("tier") in ("pro", "enterprise") and data.get("valid"):
                if needs_revalidation(data):
                    result = revalidate_license(data)
                    data = result["updated_data"]
                    if result["status"] == "expired":
                        return {"tier": "free", "valid": True, "revoked": True,
                                "reason": result.get("reason", "License expired.")}
            return data
        except Exception:
            return {"tier": "free", "valid": True}

    def needs_revalidation(data: dict) -> bool:
        if data.get("tier") not in ("pro", "enterprise"):
            return False
        last_validated = data.get("last_validated_at", data.get("activated_at", 0))
        if last_validated == 0:
            return True
        return (time.time() - last_validated) > REVALIDATION_INTERVAL

    def revalidate_license(data: dict) -> dict:
        import hashlib
        import urllib.request
        key = data.get("key", "")
        if not key:
            data["last_validated_at"] = time.time()
            data["validation_status"] = "current"
            _write_license(data)
            return {"status": "valid", "updated_data": data}

        last_validated = data.get("last_validated_at", data.get("activated_at", 0))
        elapsed = time.time() - last_validated
        machine_hash = data.get("machine_hash", hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16])

        api_valid = None
        try:
            req_data = json.dumps({"license_key": key, "instance_name": machine_hash}).encode()
            req = urllib.request.Request(
                "https://api.lemonsqueezy.com/v1/licenses/validate",
                data=req_data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            api_valid = result.get("valid", False)
        except Exception:
            api_valid = None

        if api_valid is True:
            data["last_validated_at"] = time.time()
            data["validation_status"] = "current"
            data.pop("grace_days_remaining", None)
            _write_license(data)
            return {"status": "valid", "updated_data": data}

        if elapsed > REVALIDATION_INTERVAL + HARD_BLOCK:
            data["validation_status"] = "expired"
            data["valid"] = False
            _write_license(data)
            return {"status": "expired", "updated_data": data,
                    "reason": "License expired — no successful re-validation in 44 days."}

        if elapsed > REVALIDATION_INTERVAL + GRACE_PERIOD:
            days_left = max(0, int((REVALIDATION_INTERVAL + HARD_BLOCK - elapsed) / 86400))
            data["validation_status"] = "grace_period"
            data["grace_days_remaining"] = days_left
            _write_license(data)
            return {"status": "grace", "updated_data": data, "grace_days_remaining": days_left}

        data["validation_status"] = "revalidation_pending"
        _write_license(data)
        return {"status": "grace", "updated_data": data}

    def is_license_valid(data: dict) -> bool:
        if data.get("tier") not in ("pro", "enterprise"):
            return False
        if not data.get("valid", False):
            return False
        last_validated = data.get("last_validated_at", data.get("activated_at", 0))
        if last_validated == 0:
            return True
        elapsed = time.time() - last_validated
        return elapsed <= (REVALIDATION_INTERVAL + HARD_BLOCK)

    def _write_license(data: dict) -> None:
        try:
            LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            LICENSE_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def is_premium() -> bool:
        lic = get_license()
        return is_license_valid(lic)

    def require_premium(tool_name: str) -> dict | None:
        full_name = tool_name if tool_name.startswith("delimit_") else f"delimit_{tool_name}"
        if full_name not in PRO_TOOLS:
            return None
        if is_premium():
            return None
        return {
            "error": f"'{tool_name}' requires Delimit Pro.",
            "status": "premium_required",
            "tool": tool_name,
            "current_tier": get_license().get("tier", "free"),
            "upgrade": "https://delimit.ai/pricing",
            "activate": "npx delimit-cli activate YOUR_KEY",
            "free_alternatives": [
                "delimit_lint — check API specs for free",
                "delimit_diff — compare two specs",
                "delimit_scan — discover what Delimit can do",
                "delimit_ledger_add — track tasks across sessions",
                "delimit_quickstart — guided first-run",
            ],
        }

    def activate_license(key: str) -> dict:
        # Pure-Python fallback activation (only runs when the compiled
        # license_core is unavailable). Pro is granted ONLY when Lemon
        # Squeezy confirms the key — there is no signed offline license
        # format, so an unverifiable key is NEVER granted Pro (LED-3809 /
        # SR-6 F3). Previously this path wrote tier=pro on ANY well-formed
        # >=10-char key with no network call at all — a Pro-access bypass.
        import re
        import hashlib
        import urllib.request
        if not key or len(key) < 10:
            return {"error": "Invalid license key format"}
        if key.startswith("DELIMIT-") and not re.match(r"^DELIMIT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$", key):
            return {"error": "Invalid key format. Expected: DELIMIT-XXXX-XXXX-XXXX"}

        machine_hash = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
        api_valid = None
        try:
            req_data = json.dumps({"license_key": key, "instance_name": machine_hash}).encode()
            req = urllib.request.Request(
                "https://api.lemonsqueezy.com/v1/licenses/validate",
                data=req_data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            api_valid = result.get("valid", False)
        except Exception:
            api_valid = None

        if api_valid is True:
            license_data = {
                "key": key, "tier": "pro", "valid": True,
                "activated_at": time.time(), "last_validated_at": time.time(),
                "machine_hash": machine_hash,
                "validated_via": "lemon_squeezy",
            }
            LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
            return {"status": "activated", "tier": "pro"}

        if api_valid is False:
            return {"error": "Invalid license key.", "status": "invalid"}

        # Unreachable — do NOT grant Pro on an unverified key and do NOT
        # clobber any existing license already on disk.
        return {
            "status": "pending",
            "tier": "free",
            "error": "Could not reach the license server to verify this key. "
                     "Pro was not activated. Reconnect and run activation again.",
            "message": "License validation unavailable (offline). Pro not granted — retry when online.",
        }

# ─── LED-1254 (P0 SECURITY) ──────────────────────────────────────────────
# The DELIMIT_TEST_MODE bypass that previously lived here was removed:
# it allowed any user who grepped the installed source to set
# DELIMIT_TEST_MODE=1 and unconditionally bypass every Pro license check.
# Test-time bypass is now provided exclusively by tests/conftest.py via
# monkeypatch on require_premium / is_premium. The shipped library no
# longer reads any test-mode env var.
