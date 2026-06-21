"""Secrets broker — JIT credential access with audit (STR-049).

Agents request credentials through this broker instead of accessing API keys
directly. The broker validates scope, issues time-limited access, and logs
every request in the audit trail.
"""
import json
import os
import base64
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

SECRETS_DIR = Path.home() / ".delimit" / "secrets"

# The honest at-rest field name. Values are base64-ENCODED (reversible), not
# encrypted — the field was historically misnamed "encrypted_value". We write
# the honest key and READ both for back-compat with pre-rename files (LED-1736).
_VALUE_KEY = "encoded_value"
_LEGACY_VALUE_KEY = "encrypted_value"


def _read_stored_value(secret: Dict) -> Optional[str]:
    """Return the base64 at-rest value, honest key first, legacy key fallback."""
    enc = secret.get(_VALUE_KEY)
    if enc is None:
        enc = secret.get(_LEGACY_VALUE_KEY)
    return enc


def _scope_failclosed() -> bool:
    """Finding-B / LED-1736 kill-switch. When set, an UNIDENTIFIED requester (no
    agent_type and no tool) is DENIED a non-"all"-scoped secret (fail-closed).
    Default OFF so real callers are measured first via the Phase-0 telemetry
    marker; flip ON per-host via env once the access log shows no legitimate
    dependency. Reversible — never breaks an existing caller silently."""
    return os.environ.get("DELIMIT_SECRETS_SCOPE_FAILCLOSED", "").strip().lower() in (
        "1", "true", "yes", "on")


def store_secret(
    name: str,
    value: str,
    scope: str = "all",
    description: str = "",
    created_by: str = "",
) -> Dict:
    """Store a secret locally.

    Args:
        name: Unique identifier for the secret.
        value: The secret value (will be base64-encoded at rest).
        scope: Comma-separated list of tools/agents allowed access, or 'all'.
        description: Human-readable description.
        created_by: Identity of the creator.

    Returns:
        Confirmation dict with the stored secret name.
    """
    if not name or not name.strip():
        return {"error": "Secret name is required"}
    if not value:
        return {"error": "Secret value is required"}

    # Sanitise name for filesystem safety
    safe_name = name.strip().replace("/", "_").replace("\\", "_")

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(value.encode()).decode()
    secret = {
        "name": safe_name,
        _VALUE_KEY: encoded,  # honest name; readers fall back to the legacy key
        "scope": scope,
        "description": description,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_accessed_at": None,
        "access_count": 0,
        "revoked": False,
    }
    (SECRETS_DIR / f"{safe_name}.json").write_text(json.dumps(secret, indent=2))
    return {"stored": safe_name}


def get_secret(
    name: str,
    agent_type: str = "",
    tool: str = "",
) -> Dict:
    """Request access to a secret. Returns value if authorised.

    Args:
        name: The secret name to retrieve.
        agent_type: Identity of the requesting agent (e.g. 'claude', 'codex').
        tool: Which MCP tool is requesting access.

    Returns:
        Dict with 'value' and 'granted': True on success, or 'error' and
        'granted': False on failure.
    """
    safe_name = name.strip().replace("/", "_").replace("\\", "_")
    path = SECRETS_DIR / f"{safe_name}.json"
    if not path.exists():
        _log_access(safe_name, agent_type, tool, granted=False, reason="not_found")
        return {"error": f"Secret '{safe_name}' not found", "granted": False}

    secret = json.loads(path.read_text())

    if secret.get("revoked"):
        _log_access(safe_name, agent_type, tool, granted=False, reason="revoked")
        return {"error": f"Secret '{safe_name}' has been revoked", "granted": False}

    # Scope check — 'all' allows any requester, otherwise match tool or agent_type
    scope = secret.get("scope", "all")
    grant_reason = ""
    if scope != "all":
        allowed = {s.strip().lower() for s in scope.split(",")}
        requester_ids = {agent_type.lower(), tool.lower()} - {""}
        if not requester_ids:
            # Finding-B (LED-1736): an UNIDENTIFIED caller (no agent_type/tool)
            # against a NON-"all" scope. Historically this fell OPEN (granted)
            # because the old guard was `if requester_ids and ...`. Phase 1:
            # DENY when the kill-switch is set. Default stays fail-open with a
            # Phase-0 telemetry marker so real callers are measured before the
            # default is flipped — never break an existing caller silently.
            if _scope_failclosed():
                _log_access(
                    safe_name, agent_type, tool,
                    granted=False,
                    reason=f"scope_denied: unidentified requester for scope '{scope}' (fail-closed)",
                )
                return {
                    "error": (f"Access denied: scope '{scope}' requires an "
                              "identified requester (pass agent_type or tool)"),
                    "granted": False,
                }
            grant_reason = (
                f"scope_warn: unidentified requester granted under legacy "
                f"fail-open for scope '{scope}' "
                "(set DELIMIT_SECRETS_SCOPE_FAILCLOSED=1 to deny)")
        elif not requester_ids & allowed:
            _log_access(
                safe_name, agent_type, tool,
                granted=False,
                reason=f"scope_denied: required={scope}, got agent_type={agent_type}, tool={tool}",
            )
            return {
                "error": f"Access denied: scope '{scope}' does not include '{agent_type or tool}'",
                "granted": False,
            }

    # Read the at-rest value (honest key first, legacy key fallback).
    enc = _read_stored_value(secret)
    if enc is None:
        _log_access(safe_name, agent_type, tool, granted=False, reason="corrupt: no stored value")
        return {"error": f"Secret '{safe_name}' is corrupt (no stored value)", "granted": False}

    # Log successful access (grant_reason carries the Phase-0 telemetry marker
    # when an unidentified requester was granted under legacy fail-open).
    _log_access(safe_name, agent_type, tool, granted=True, reason=grant_reason)

    # Update access metadata
    secret["access_count"] = secret.get("access_count", 0) + 1
    secret["last_accessed_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(secret, indent=2))

    value = base64.b64decode(enc).decode()
    return {"value": value, "granted": True, "name": safe_name}


def list_secrets() -> List[Dict]:
    """List all secrets (metadata only, never values).

    Returns:
        List of dicts with name, scope, description, access_count, etc.
    """
    if not SECRETS_DIR.exists():
        return []
    secrets = []
    for f in sorted(SECRETS_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue  # skip internal files like _access_log.json
        try:
            s = json.loads(f.read_text())
            if not isinstance(s, dict):
                continue
            secrets.append({
                "name": s.get("name", f.stem),
                "scope": s.get("scope", "all"),
                "description": s.get("description", ""),
                "created_by": s.get("created_by", ""),
                "access_count": s.get("access_count", 0),
                "last_accessed_at": s.get("last_accessed_at"),
                "revoked": s.get("revoked", False),
                "created_at": s.get("created_at", ""),
            })
        except (json.JSONDecodeError, KeyError):
            pass
    return secrets


def revoke_secret(name: str) -> Dict:
    """Revoke a secret, preventing future access.

    Args:
        name: The secret name to revoke.

    Returns:
        Confirmation dict or error.
    """
    safe_name = name.strip().replace("/", "_").replace("\\", "_")
    path = SECRETS_DIR / f"{safe_name}.json"
    if not path.exists():
        return {"error": f"Secret '{safe_name}' not found"}
    secret = json.loads(path.read_text())
    secret["revoked"] = True
    secret["revoked_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(secret, indent=2))
    _log_access(safe_name, "", "", granted=True, reason="revoked_by_user")
    return {"revoked": safe_name}


def get_access_log(name: Optional[str] = None) -> List[Dict]:
    """Return access log entries, optionally filtered by secret name.

    Args:
        name: If provided, only return entries for this secret.

    Returns:
        List of access log entries (newest first).
    """
    log_path = SECRETS_DIR / "access_log" / "log.jsonl"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if name and entry.get("secret_name") != name:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            pass
    # Newest first
    entries.reverse()
    return entries


def delete_secret(name: str) -> Dict:
    """Permanently delete a secret file.

    Args:
        name: The secret name to delete.

    Returns:
        Confirmation dict or error.
    """
    safe_name = name.strip().replace("/", "_").replace("\\", "_")
    path = SECRETS_DIR / f"{safe_name}.json"
    if not path.exists():
        return {"error": f"Secret '{safe_name}' not found"}
    path.unlink()
    _log_access(safe_name, "", "", granted=True, reason="deleted_by_user")
    return {"deleted": safe_name}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log_access(
    secret_name: str,
    agent_type: str,
    tool: str,
    granted: bool,
    reason: str,
) -> None:
    """Append an entry to the JSONL access log."""
    log_dir = SECRETS_DIR / "access_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "secret_name": secret_name,
        "agent_type": agent_type,
        "tool": tool,
        "ts": datetime.now(timezone.utc).isoformat(),
        "granted": granted,
        "reason": reason,
    }
    with open(log_dir / "log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
