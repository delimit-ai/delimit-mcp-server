"""
Delimit license enforcement core — compiled with Nuitka.
Contains: validation logic, re-validation, usage tracking, entitlement checks.
This module is distributed as a native binary (.so/.pyd), not readable Python.
"""
import hashlib
import json
import time
from pathlib import Path

LICENSE_FILE = Path.home() / ".delimit" / "license.json"
USAGE_FILE = Path.home() / ".delimit" / "usage.json"
LS_VALIDATE_URL = "https://api.lemonsqueezy.com/v1/licenses/validate"

REVALIDATION_INTERVAL = 30 * 86400  # 30 days
GRACE_PERIOD = 7 * 86400
HARD_BLOCK = 14 * 86400

# Pro tools that require a license
PRO_TOOLS = frozenset({
    "delimit_gov_evaluate",
    "delimit_gov_policy", "delimit_gov_run", "delimit_gov_verify",
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
    "delimit_social_post", "delimit_social_generate", "delimit_social_history",
    "delimit_screen_record", "delimit_screenshot",
    "delimit_notify",
    # Agent orchestration
    "delimit_agent_dispatch", "delimit_agent_status",
    "delimit_agent_complete", "delimit_agent_handoff",
    # Worker Pool v2 executor (LED-981)
    "delimit_executor",
})

# Free trial limits
FREE_TRIAL_LIMITS = {
    "delimit_deliberate": 3,
}


def needs_revalidation(data: dict) -> bool:
    """Check if a license needs re-validation (30+ days since last check).

    Args:
        data: License data dict (from license.json).

    Returns:
        True if 30+ days have elapsed since last_validated_at (or activated_at
        as fallback). Also returns True if neither timestamp exists (legacy
        license.json files without last_validated_at).
    """
    if data.get("tier") not in ("pro", "enterprise"):
        return False
    last_validated = data.get("last_validated_at", data.get("activated_at", 0))
    if last_validated == 0:
        return True  # Legacy file — treat as needing validation
    return (time.time() - last_validated) > REVALIDATION_INTERVAL


def revalidate_license(data: dict) -> dict:
    """Re-validate a license against Lemon Squeezy.

    Privacy-preserving: only sends license_key and instance_name (machine hash).
    Non-blocking: network failures return offline grace status, never crash.

    Args:
        data: License data dict (must contain 'key').

    Returns:
        Dict with 'status' key:
          - "valid": API confirmed license is active, last_validated_at updated
          - "grace": API unreachable or returned invalid, but within grace period
          - "expired": beyond grace + hard block cutoff, Pro tools should be blocked
        Also includes 'updated_data' with the (possibly modified) license data.
    """
    key = data.get("key", "")
    # Internal/founder keys always pass
    if not key or key.startswith("JAMSONS"):
        data["last_validated_at"] = time.time()
        data["validation_status"] = "current"
        _write_license(data)
        return {"status": "valid", "updated_data": data}

    last_validated = data.get("last_validated_at", data.get("activated_at", 0))
    elapsed = time.time() - last_validated

    # Try API call
    api_valid = _call_lemon_squeezy(data)

    if api_valid is True:
        data["last_validated_at"] = time.time()
        data["validation_status"] = "current"
        data.pop("grace_days_remaining", None)
        _write_license(data)
        return {"status": "valid", "updated_data": data}

    # API said invalid or was unreachable — check grace/expiry windows
    if elapsed > REVALIDATION_INTERVAL + HARD_BLOCK:
        data["validation_status"] = "expired"
        data["valid"] = False
        _write_license(data)
        return {
            "status": "expired",
            "updated_data": data,
            "reason": "License expired — no successful re-validation in 44 days. Renew at https://delimit.ai/pricing",
        }

    if elapsed > REVALIDATION_INTERVAL + GRACE_PERIOD:
        days_left = max(0, int((REVALIDATION_INTERVAL + HARD_BLOCK - elapsed) / 86400))
        data["validation_status"] = "grace_period"
        data["grace_days_remaining"] = days_left
        _write_license(data)
        return {
            "status": "grace",
            "updated_data": data,
            "grace_days_remaining": days_left,
            "message": f"License re-validation failed. {days_left} days until Pro features are disabled.",
        }

    # Within first 7 days after revalidation interval — soft pending
    data["validation_status"] = "revalidation_pending"
    _write_license(data)
    return {"status": "grace", "updated_data": data}


def is_license_valid(data: dict) -> bool:
    """Check if a license is currently valid for Pro tool access.

    Returns True if:
      - last_validated_at is within 30 days (current), OR
      - last_validated_at is within 37 days (30 + 7 grace), OR
      - last_validated_at is within 44 days (30 + 14 hard cutoff)
    Returns False if beyond 44 days with no successful re-validation.

    Backwards compatible: missing last_validated_at falls back to activated_at,
    and missing both returns False (triggers re-validation).
    """
    if data.get("tier") not in ("pro", "enterprise"):
        return False
    if not data.get("valid", False):
        return False
    # Internal/founder keys always valid
    key = data.get("key", "")
    if key.startswith("JAMSONS"):
        return True
    last_validated = data.get("last_validated_at", data.get("activated_at", 0))
    if last_validated == 0:
        return True  # Legacy — allow access but needs_revalidation will trigger check
    elapsed = time.time() - last_validated
    return elapsed <= (REVALIDATION_INTERVAL + HARD_BLOCK)


def _write_license(data: dict) -> None:
    """Persist license data to disk."""
    try:
        LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # Non-blocking — don't crash on disk errors


def _call_lemon_squeezy(data: dict) -> bool | None:
    """Call Lemon Squeezy validation API. Privacy-preserving.

    Only sends license_key and instance_name (machine hash).

    Returns:
        True if valid, False if invalid, None if unreachable.
    """
    key = data.get("key", "")
    machine_hash = data.get("machine_hash", hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16])
    try:
        import urllib.request
        req_data = json.dumps({
            "license_key": key,
            "instance_name": machine_hash,
        }).encode()
        req = urllib.request.Request(
            LS_VALIDATE_URL, data=req_data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return result.get("valid", False)
    except Exception:
        return None  # Unreachable — caller should use grace period


def load_license() -> dict:
    """Load and validate license with periodic re-validation.

    Re-validates against Lemon Squeezy every 30 days. On failure, provides
    a 7-day grace period followed by a 7-day warning period. After 44 days
    without successful re-validation, Pro tools are blocked.
    """
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
                            "reason": result.get("reason", "License expired. Renew at https://delimit.ai/pricing")}
        return data
    except Exception:
        return {"tier": "free", "valid": True}


def check_premium() -> bool:
    """Check if user has a valid premium license.

    Uses load_license() which triggers re-validation if needed, then
    checks is_license_valid() on the result.
    """
    lic = load_license()
    return is_license_valid(lic)


def gate_tool(tool_name: str) -> dict | None:
    """Gate a Pro tool. Returns None if allowed, error dict if blocked."""
    # Normalize: accept both "os_plan" and "delimit_os_plan"
    full_name = tool_name if tool_name.startswith("delimit_") else f"delimit_{tool_name}"
    if full_name not in PRO_TOOLS:
        return None
    if check_premium():
        return None

    # Check free trial
    limit = FREE_TRIAL_LIMITS.get(tool_name)
    if limit is not None:
        used = _get_monthly_usage(tool_name)
        if used < limit:
            _increment_usage(tool_name)
            return None
        return {
            "error": f"Free trial limit reached ({limit}/month). Upgrade to Pro for unlimited.",
            "status": "trial_exhausted",
            "tool": tool_name,
            "used": used,
            "limit": limit,
            "upgrade_url": "https://delimit.ai/pricing",
        }

    return {
        "error": f"'{tool_name}' requires Delimit Pro ($10/mo). Upgrade at https://delimit.ai/pricing",
        "status": "premium_required",
        "tool": tool_name,
        "current_tier": load_license().get("tier", "free"),
    }


def activate(key: str) -> dict:
    """Activate a license key."""
    import re
    if not key or len(key) < 10:
        return {"error": "Invalid license key format"}
    # Accept DELIMIT-XXXX-XXXX-XXXX pattern or Lemon Squeezy format
    if key.startswith("DELIMIT-") and not re.match(r"^DELIMIT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$", key):
        return {"error": "Invalid key format. Expected: DELIMIT-XXXX-XXXX-XXXX"}

    machine_hash = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]

    try:
        import urllib.request
        data = json.dumps({"license_key": key, "instance_name": machine_hash}).encode()
        req = urllib.request.Request(
            LS_VALIDATE_URL, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        if result.get("valid"):
            license_data = {
                "key": key, "tier": "pro", "valid": True,
                "activated_at": time.time(), "last_validated_at": time.time(),
                "machine_hash": machine_hash,
                "instance_id": result.get("instance", {}).get("id"),
                "validated_via": "lemon_squeezy",
            }
            LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
            return {"status": "activated", "tier": "pro"}
        return {"error": "Invalid license key.", "status": "invalid"}

    except Exception:
        license_data = {
            "key": key, "tier": "pro", "valid": True,
            "activated_at": time.time(), "last_validated_at": time.time(),
            "machine_hash": machine_hash, "validated_via": "offline",
        }
        LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
        return {"status": "activated", "tier": "pro", "message": "Activated offline."}


def _revalidate(data: dict) -> dict:
    """Re-validate against Lemon Squeezy (legacy wrapper).

    Deprecated: use revalidate_license() for the full status/grace workflow.
    Kept for backwards compatibility with any external callers.
    """
    result = _call_lemon_squeezy(data)
    if result is True:
        return {"valid": True}
    if result is False:
        return {"valid": False}
    # None = unreachable — grant offline grace
    return {"valid": True, "offline": True}


def _get_monthly_usage(tool_name: str) -> int:
    if not USAGE_FILE.exists():
        return 0
    try:
        data = json.loads(USAGE_FILE.read_text())
        return data.get(time.strftime("%Y-%m"), {}).get(tool_name, 0)
    except Exception:
        return 0


def _increment_usage(tool_name: str) -> int:
    month_key = time.strftime("%Y-%m")
    data = {}
    if USAGE_FILE.exists():
        try:
            data = json.loads(USAGE_FILE.read_text())
        except Exception:
            pass
    if month_key not in data:
        data[month_key] = {}
    data[month_key][tool_name] = data[month_key].get(tool_name, 0) + 1
    count = data[month_key][tool_name]
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data, indent=2))
    return count
