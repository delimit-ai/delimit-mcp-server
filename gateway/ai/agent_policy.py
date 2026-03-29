"""Per-model policy scoping — agent-level governance.

Allows setting different permissions per AI model:
- Which tools each model can call
- Read-only vs read-write access to ledger/memory
- Deploy permissions per model
- Custom constraints per agent identity

Storage: ~/.delimit/agents/policies.json

Feedback origin: Accurate_Mistake_398 on r/ClaudeAI (2026-03-28)
identified that governance was session-level, not agent-level.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENTS_DIR = Path.home() / ".delimit" / "agents"
POLICIES_FILE = AGENTS_DIR / "policies.json"

# Default permissions — what each model gets if no policy is set
DEFAULT_PERMISSIONS = {
    "ledger": "read-write",
    "memory": "read-write",
    "deploy": False,
    "lint": True,
    "deliberate": True,
    "security_audit": True,
    "evidence": "read-write",
    "secrets": False,
}

VALID_MODELS = {"claude", "codex", "gemini", "cursor", "any"}
VALID_ACCESS = {"read-only", "read-write", "none"}


def _ensure_dir():
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_policies() -> Dict[str, Any]:
    if not POLICIES_FILE.exists():
        return {}
    try:
        return json.loads(POLICIES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_policies(policies: Dict[str, Any]):
    _ensure_dir()
    POLICIES_FILE.write_text(json.dumps(policies, indent=2))


def set_agent_policy(
    model: str,
    ledger: str = "",
    memory: str = "",
    deploy: Optional[bool] = None,
    lint: Optional[bool] = None,
    deliberate: Optional[bool] = None,
    security_audit: Optional[bool] = None,
    evidence: str = "",
    secrets: Optional[bool] = None,
    custom_constraints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Set permissions for a specific AI model.

    Example: set_agent_policy("codex", ledger="read-only", deploy=False)
    means Codex can read the ledger but not write, and cannot deploy.
    """
    model = model.lower().strip()
    if model not in VALID_MODELS:
        return {"error": f"model must be one of: {', '.join(sorted(VALID_MODELS))}"}

    policies = _load_policies()
    existing = policies.get(model, dict(DEFAULT_PERMISSIONS))

    if ledger and ledger in VALID_ACCESS:
        existing["ledger"] = ledger
    if memory and memory in VALID_ACCESS:
        existing["memory"] = memory
    if evidence and evidence in VALID_ACCESS:
        existing["evidence"] = evidence
    if deploy is not None:
        existing["deploy"] = deploy
    if lint is not None:
        existing["lint"] = lint
    if deliberate is not None:
        existing["deliberate"] = deliberate
    if security_audit is not None:
        existing["security_audit"] = security_audit
    if secrets is not None:
        existing["secrets"] = secrets
    if custom_constraints is not None:
        existing["custom_constraints"] = custom_constraints

    existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    policies[model] = existing
    _save_policies(policies)

    return {
        "status": "updated",
        "model": model,
        "policy": existing,
        "message": f"Policy updated for {model}",
    }


def get_agent_policy(model: str = "") -> Dict[str, Any]:
    """Get permissions for a specific model, or all models."""
    policies = _load_policies()

    if not model or not model.strip():
        # Return all policies with defaults filled in
        all_policies = {}
        for m in VALID_MODELS - {"any"}:
            all_policies[m] = policies.get(m, dict(DEFAULT_PERMISSIONS))
        return {
            "status": "ok",
            "policies": all_policies,
            "default": DEFAULT_PERMISSIONS,
        }

    model = model.lower().strip()
    if model not in VALID_MODELS:
        return {"error": f"model must be one of: {', '.join(sorted(VALID_MODELS))}"}

    policy = policies.get(model, dict(DEFAULT_PERMISSIONS))
    return {
        "status": "ok",
        "model": model,
        "policy": policy,
        "is_default": model not in policies,
    }


def check_agent_permission(
    model: str,
    action: str,
    resource: str = "",
) -> Dict[str, Any]:
    """Check if a model is allowed to perform an action.

    Actions: ledger_write, ledger_read, memory_write, memory_read,
             deploy, lint, deliberate, security_audit, evidence_write,
             evidence_read, secrets_read, secrets_write.

    Returns: {"allowed": bool, "reason": str}
    """
    model = model.lower().strip() if model else "any"
    policies = _load_policies()
    policy = policies.get(model, dict(DEFAULT_PERMISSIONS))

    action = action.lower().strip()

    # Parse action into category + operation
    if "_" in action:
        parts = action.split("_", 1)
        category = parts[0]
        operation = parts[1] if len(parts) > 1 else "read"
    else:
        category = action
        operation = "read"

    # Check access-level permissions (ledger, memory, evidence)
    if category in ("ledger", "memory", "evidence"):
        access = policy.get(category, "read-write")
        if access == "none":
            return {
                "allowed": False,
                "model": model,
                "action": action,
                "reason": f"{model} has no access to {category}",
            }
        if operation == "write" and access == "read-only":
            return {
                "allowed": False,
                "model": model,
                "action": action,
                "reason": f"{model} has read-only access to {category}",
            }
        return {"allowed": True, "model": model, "action": action, "reason": "permitted"}

    # Check boolean permissions (deploy, lint, etc.)
    if category in ("deploy", "lint", "deliberate", "security_audit", "secrets"):
        key = category.replace("security_", "security_")
        allowed = policy.get(key, DEFAULT_PERMISSIONS.get(key, True))
        if not allowed:
            return {
                "allowed": False,
                "model": model,
                "action": action,
                "reason": f"{model} is not permitted to {category}",
            }
        return {"allowed": True, "model": model, "action": action, "reason": "permitted"}

    # Check custom constraints
    constraints = policy.get("custom_constraints", [])
    for c in constraints:
        c_lower = c.lower().strip()
        if c_lower.startswith("no-") and c_lower[3:] in action:
            return {
                "allowed": False,
                "model": model,
                "action": action,
                "reason": f"Custom constraint: {c}",
            }

    return {"allowed": True, "model": model, "action": action, "reason": "no restrictions"}


def remove_agent_policy(model: str) -> Dict[str, Any]:
    """Remove custom policy for a model, reverting to defaults."""
    model = model.lower().strip()
    policies = _load_policies()

    if model not in policies:
        return {"status": "ok", "message": f"No custom policy for {model} (already using defaults)"}

    del policies[model]
    _save_policies(policies)

    return {
        "status": "removed",
        "model": model,
        "message": f"Custom policy removed for {model}. Now using defaults.",
    }
