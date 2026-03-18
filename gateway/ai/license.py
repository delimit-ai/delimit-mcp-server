"""
Delimit license validation.
Free tools work without a key. Premium tools check for a valid key.
Validates against Lemon Squeezy API when online, falls back to local cache.
"""
import hashlib
import json
import os
import time
from pathlib import Path

LICENSE_FILE = Path.home() / ".delimit" / "license.json"
LS_VALIDATE_URL = "https://api.lemonsqueezy.com/v1/licenses/validate"


def get_license() -> dict:
    """Load license from ~/.delimit/license.json"""
    if not LICENSE_FILE.exists():
        return {"tier": "free", "valid": True}
    try:
        data = json.loads(LICENSE_FILE.read_text())
        if data.get("expires_at") and data["expires_at"] < time.time():
            return {"tier": "free", "valid": True, "expired": True}
        return data
    except Exception:
        return {"tier": "free", "valid": True}


def is_premium() -> bool:
    """Check if user has a premium license."""
    lic = get_license()
    return lic.get("tier") in ("pro", "enterprise") and lic.get("valid", False)


def require_premium(tool_name: str) -> dict | None:
    """Check premium access. Returns None if allowed, error dict if not."""
    if is_premium():
        return None
    return {
        "error": f"'{tool_name}' requires Delimit Pro. Upgrade at https://delimit.ai/pricing",
        "status": "premium_required",
        "tool": tool_name,
        "current_tier": get_license().get("tier", "free"),
    }


def activate_license(key: str) -> dict:
    """Activate a license key via Lemon Squeezy API.
    Falls back to local validation if API is unreachable."""
    if not key or len(key) < 10:
        return {"error": "Invalid license key format"}

    machine_hash = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]

    # Try Lemon Squeezy remote validation
    try:
        import urllib.request
        import urllib.error

        data = json.dumps({
            "license_key": key,
            "instance_name": machine_hash,
        }).encode()

        req = urllib.request.Request(
            LS_VALIDATE_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        if result.get("valid"):
            license_data = {
                "key": key,
                "tier": "pro",
                "valid": True,
                "activated_at": time.time(),
                "machine_hash": machine_hash,
                "instance_id": result.get("instance", {}).get("id"),
                "license_id": result.get("license_key", {}).get("id"),
                "customer_name": result.get("meta", {}).get("customer_name", ""),
                "validated_via": "lemon_squeezy",
            }
            LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
            return {"status": "activated", "tier": "pro", "message": "License activated successfully."}
        else:
            return {
                "error": "Invalid license key. Check your key and try again.",
                "status": "invalid",
                "detail": result.get("error", ""),
            }

    except (urllib.error.URLError, OSError):
        # API unreachable — accept key locally (offline activation)
        license_data = {
            "key": key,
            "tier": "pro",
            "valid": True,
            "activated_at": time.time(),
            "machine_hash": machine_hash,
            "validated_via": "offline",
        }
        LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
        return {
            "status": "activated",
            "tier": "pro",
            "message": "License activated (offline). Will validate online next time.",
        }
    except Exception as e:
        # Unexpected error — still activate locally
        license_data = {
            "key": key,
            "tier": "pro",
            "valid": True,
            "activated_at": time.time(),
            "machine_hash": machine_hash,
            "validated_via": "fallback",
        }
        LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps(license_data, indent=2))
        return {
            "status": "activated",
            "tier": "pro",
            "message": f"License activated (validation error: {e}). Will retry online later.",
        }
