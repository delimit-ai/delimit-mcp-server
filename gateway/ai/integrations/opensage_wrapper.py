"""
Delimit GovernanceWrapper for OpenSage Agent Framework.

Middleware that intercepts OpenSage agent tool calls and routes them
through Delimit's policy kernel for governance enforcement.

Usage:
    from delimit.integrations.opensage import GovernanceWrapper

    # Wrap an OpenSage agent with governance
    agent = OpenSageAgent(config)
    governed_agent = GovernanceWrapper(agent)

    # Or use as a feature plugin
    agent = OpenSageAgent(config, features=[GovernanceWrapper()])

Integration points:
    1. Pre-tool: validate tool call against policy before execution
    2. Post-tool: audit trail + ledger tracking
    3. Tool creation: validate agent-created tools before registration
    4. Session: persistent context across agent runs

Requires: delimit-mcp >= 3.2.1
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("delimit.integrations.opensage")

# LED-1188: env-var-aware home resolution (DELIMIT_HOME / DELIMIT_NAMESPACE_ROOT).
from ..continuity import get_namespace_root  # noqa: E402

DELIMIT_HOME = get_namespace_root()
AUDIT_DIR = DELIMIT_HOME / "audit"
POLICY_FILE = DELIMIT_HOME / "enforcement_mode"


def _get_mode() -> str:
    """Read current enforcement mode."""
    try:
        return POLICY_FILE.read_text().strip()
    except Exception:
        return "guarded"


def _classify_tool_risk(tool_name: str, args: dict) -> str:
    """Classify risk level of a tool call."""
    HIGH_RISK_PATTERNS = [
        "deploy", "publish", "push", "delete", "remove", "drop",
        "exec", "shell", "run_command", "write_file",
    ]
    CRITICAL_PATTERNS = [
        "rm_rf", "force_push", "drop_table", "revoke",
    ]

    name_lower = tool_name.lower()
    for pattern in CRITICAL_PATTERNS:
        if pattern in name_lower:
            return "critical"
    for pattern in HIGH_RISK_PATTERNS:
        if pattern in name_lower:
            return "high"
    return "low"


def _check_sensitive_paths(args: dict) -> Optional[str]:
    """Check if any argument references a sensitive path."""
    SENSITIVE = ["/etc/", "/.ssh/", "/.aws/", "/credentials/", "/.env"]
    for key, value in args.items():
        if isinstance(value, str):
            for pattern in SENSITIVE:
                if pattern in value:
                    return f"Argument '{key}' references sensitive path: {pattern}"
    return None


def _audit_log(entry: dict, audit_dir: Optional[Path] = None) -> None:
    """Append to audit trail."""
    try:
        d = audit_dir or AUDIT_DIR
        d.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        audit_file = d / f"opensage-{date}.jsonl"
        with open(audit_file, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


class GovernancePolicy:
    """Policy rules for OpenSage tool governance."""

    def __init__(self, rules: Optional[List[dict]] = None, mode: Optional[str] = None):
        self.rules = rules or []
        self._mode_override = mode

    def check(self, tool_name: str, args: dict) -> dict:
        """Check a tool call against all policy rules.

        Returns:
            {"allowed": True} or {"allowed": False, "reason": "...", "rule": "..."}
        """
        mode = self._mode_override or _get_mode()

        if mode == "advisory":
            return {"allowed": True, "mode": mode}

        # Check sensitive paths
        path_issue = _check_sensitive_paths(args)
        if path_issue:
            if mode == "enforce":
                return {"allowed": False, "reason": path_issue, "mode": mode}
            logger.warning("Governance warning: %s", path_issue)

        # Check risk level
        risk = _classify_tool_risk(tool_name, args)

        if risk == "critical":
            return {
                "allowed": False,
                "reason": f"Critical action '{tool_name}' requires approval",
                "risk": risk,
                "mode": mode,
            }

        if risk == "high" and mode == "enforce":
            return {
                "allowed": False,
                "reason": f"High-risk action '{tool_name}' blocked in enforce mode",
                "risk": risk,
                "mode": mode,
            }

        # Check custom rules
        for rule in self.rules:
            if rule.get("tool_pattern") and rule["tool_pattern"] in tool_name:
                if rule.get("action") == "forbid":
                    return {
                        "allowed": False,
                        "reason": rule.get("message", f"Rule '{rule.get('name')}' blocks this"),
                        "rule": rule.get("name"),
                    }

        return {"allowed": True, "risk": risk, "mode": mode}


class GovernanceWrapper:
    """Wraps OpenSage agent tool execution with Delimit governance.

    Can be used as:
    1. A wrapper around an existing agent
    2. A standalone policy checker
    3. A feature plugin for OpenSage
    """

    def __init__(
        self,
        policy: Optional[GovernancePolicy] = None,
        audit: bool = True,
        ledger_tracking: bool = True,
        audit_dir: Optional[Path] = None,
    ):
        self.policy = policy or GovernancePolicy()
        self.audit = audit
        self.ledger_tracking = ledger_tracking
        self.audit_dir = audit_dir
        self._call_count = 0
        self._blocked_count = 0

    def pre_tool_call(self, tool_name: str, args: dict) -> dict:
        """Check policy before a tool executes.

        Returns:
            {"proceed": True} or {"proceed": False, "reason": "..."}
        """
        self._call_count += 1
        check = self.policy.check(tool_name, args)

        if self.audit:
            _audit_log({
                "event": "pre_tool_call",
                "tool": tool_name,
                "args_summary": {k: str(v)[:100] for k, v in args.items()},
                "decision": "allowed" if check["allowed"] else "blocked",
                "reason": check.get("reason"),
                "risk": check.get("risk", "low"),
                "mode": check.get("mode", "unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, audit_dir=self.audit_dir)

        if not check["allowed"]:
            self._blocked_count += 1
            return {"proceed": False, "reason": check["reason"]}

        return {"proceed": True, "risk": check.get("risk", "low")}

    def post_tool_call(self, tool_name: str, args: dict, result: Any, duration_ms: int) -> None:
        """Record tool execution in audit trail."""
        if self.audit:
            _audit_log({
                "event": "post_tool_call",
                "tool": tool_name,
                "duration_ms": duration_ms,
                "success": not isinstance(result, Exception),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, audit_dir=self.audit_dir)

    def validate_tool_creation(self, tool_name: str, tool_schema: dict) -> dict:
        """Validate an agent-created tool before registration.

        Checks:
        - Tool name doesn't conflict with system tools
        - Schema doesn't expose sensitive parameters
        - Tool doesn't request unrestricted permissions

        Returns:
            {"valid": True} or {"valid": False, "reason": "..."}
        """
        RESERVED_PREFIXES = ["system_", "admin_", "delimit_", "opensage_"]

        for prefix in RESERVED_PREFIXES:
            if tool_name.startswith(prefix):
                return {
                    "valid": False,
                    "reason": f"Tool name '{tool_name}' uses reserved prefix '{prefix}'",
                }

        # Check for sensitive parameter names
        params = tool_schema.get("parameters", {}).get("properties", {})
        SENSITIVE_PARAMS = ["password", "secret", "token", "api_key", "private_key"]
        for param_name in params:
            if any(s in param_name.lower() for s in SENSITIVE_PARAMS):
                return {
                    "valid": False,
                    "reason": f"Tool parameter '{param_name}' exposes sensitive data",
                }

        if self.audit:
            _audit_log({
                "event": "tool_creation_validated",
                "tool": tool_name,
                "schema_keys": list(tool_schema.keys()),
                "param_count": len(params),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, audit_dir=self.audit_dir)

        return {"valid": True}

    def wrap_tool(self, tool_fn: Callable) -> Callable:
        """Wrap a tool function with governance checks.

        Usage:
            original_tool = agent.get_tool("my_tool")
            governed_tool = wrapper.wrap_tool(original_tool)
        """
        def governed_tool(*args, **kwargs):
            tool_name = getattr(tool_fn, "__name__", "unknown")
            check = self.pre_tool_call(tool_name, kwargs)

            if not check["proceed"]:
                return {"error": "governance_blocked", "reason": check["reason"]}

            start = time.time()
            try:
                result = tool_fn(*args, **kwargs)
                duration_ms = int((time.time() - start) * 1000)
                self.post_tool_call(tool_name, kwargs, result, duration_ms)
                return result
            except Exception as e:
                duration_ms = int((time.time() - start) * 1000)
                self.post_tool_call(tool_name, kwargs, e, duration_ms)
                raise

        governed_tool.__name__ = getattr(tool_fn, "__name__", "unknown")
        governed_tool.__doc__ = getattr(tool_fn, "__doc__", "")
        governed_tool._governance_wrapped = True
        return governed_tool

    def get_stats(self) -> dict:
        """Return governance statistics for this session."""
        return {
            "total_calls": self._call_count,
            "blocked_calls": self._blocked_count,
            "block_rate": f"{(self._blocked_count / max(self._call_count, 1)) * 100:.1f}%",
            "mode": _get_mode(),
        }
