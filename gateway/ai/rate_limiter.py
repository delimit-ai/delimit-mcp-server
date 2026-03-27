"""
Delimit MCP Rate Limiter — per-tool call limits and session cost controls.

Provides sliding-window rate limiting and cumulative cost tracking for all
MCP tools. Designed to prevent runaway agent loops from burning through
expensive API calls.

Configuration:
    ~/.delimit/rate_limits.yml — per-tool overrides
    Defaults: 100 calls/hr (free), 20 calls/hr (Pro), 5 calls/hr (deliberation)

Usage:
    from ai.rate_limiter import limiter

    block = limiter.check("delimit_lint")
    if block:
        return block  # contains error message + wait hint
    # ... execute tool ...
    limiter.record("delimit_lint", cost=0.001)
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.rate_limiter")

# ---------------------------------------------------------------------------
#  Tool tier classification
# ---------------------------------------------------------------------------

# Tools that invoke multi-model deliberation (most expensive)
DELIBERATION_TOOLS = frozenset({
    "delimit_deliberate",
    "delimit_security_deliberate",
})

# Pro tools that do significant computation but aren't deliberation
# Mirrors the PRO_TOOLS set from ai/license.py
PRO_TOOLS = frozenset({
    "delimit_gov_evaluate", "delimit_gov_policy", "delimit_gov_run",
    "delimit_gov_verify", "delimit_gov_new_task",
    "delimit_os_plan", "delimit_os_status", "delimit_os_gates",
    "delimit_deploy_plan", "delimit_deploy_build", "delimit_deploy_publish",
    "delimit_deploy_verify", "delimit_deploy_rollback", "delimit_deploy_status",
    "delimit_deploy_site", "delimit_deploy_npm",
    "delimit_memory_search",
    "delimit_vault_search", "delimit_vault_snapshot", "delimit_vault_health",
    "delimit_evidence_collect", "delimit_evidence_verify",
    "delimit_models",
    "delimit_security_ingest",
    "delimit_obs_metrics", "delimit_obs_logs", "delimit_obs_status",
    "delimit_release_plan", "delimit_release_status", "delimit_release_sync",
    "delimit_cost_analyze", "delimit_cost_optimize", "delimit_cost_alert",
    "delimit_social_post", "delimit_social_generate", "delimit_social_history",
    "delimit_repo_analyze", "delimit_repo_config_audit",
    "delimit_repo_config_validate", "delimit_repo_diagnose",
    "delimit_test_coverage",
    "delimit_screen_record", "delimit_screenshot",
    "delimit_notify",
    "delimit_agent_dispatch", "delimit_agent_status",
    "delimit_agent_complete", "delimit_agent_handoff",
})

# Per-tool cost estimates (USD).  Tools not listed default to 0.
DEFAULT_COST_ESTIMATES: Dict[str, float] = {
    # Deliberation — multiple LLM calls
    "delimit_deliberate": 0.01,
    "delimit_security_deliberate": 0.01,
    # Lint / diff / semver — local computation, minimal cost
    "delimit_lint": 0.001,
    "delimit_diff": 0.001,
    "delimit_semver": 0.001,
    # Deploy actions — infrastructure cost
    "delimit_deploy_publish": 0.005,
    "delimit_deploy_build": 0.003,
    "delimit_deploy_site": 0.005,
    "delimit_deploy_npm": 0.005,
    # Agent dispatch — orchestrates sub-agents
    "delimit_agent_dispatch": 0.008,
    # Social posting — API calls to external services
    "delimit_social_post": 0.002,
    "delimit_social_generate": 0.003,
    # Screen recording / screenshots — browser automation
    "delimit_screen_record": 0.005,
    "delimit_screenshot": 0.002,
    # Everything else is effectively free (local computation)
}

# Default hourly limits by tier
DEFAULT_LIMIT_FREE = 100
DEFAULT_LIMIT_PRO = 20
DEFAULT_LIMIT_DELIBERATION = 5

# Session cost cap
DEFAULT_SESSION_COST_CAP = 5.0

# Warning threshold (fraction of limit used before emitting a warning)
WARNING_THRESHOLD = 0.80

# Sliding window duration in seconds (1 hour)
WINDOW_SECONDS = 3600


def _classify_tool(tool_name: str) -> str:
    """Return the tier for a tool: 'deliberation', 'pro', or 'free'."""
    if tool_name in DELIBERATION_TOOLS:
        return "deliberation"
    if tool_name in PRO_TOOLS:
        return "pro"
    return "free"


def _default_limit_for(tool_name: str) -> int:
    """Return the default hourly call limit based on tool tier."""
    tier = _classify_tool(tool_name)
    if tier == "deliberation":
        return DEFAULT_LIMIT_DELIBERATION
    if tier == "pro":
        return DEFAULT_LIMIT_PRO
    return DEFAULT_LIMIT_FREE


def _load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load rate limit overrides from YAML config.

    Returns a dict with optional keys:
        session_cost_cap: float
        tools: {tool_name: {limit: int, cost: float}}
        tiers: {free: int, pro: int, deliberation: int}
    """
    if config_path is None:
        config_path = Path.home() / ".delimit" / "rate_limits.yml"

    if not config_path.exists():
        return {}

    try:
        # Use PyYAML if available; fall back to a simple parser
        try:
            import yaml
            with open(config_path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except ImportError:
            return _parse_simple_yaml(config_path)
    except Exception as exc:
        logger.warning("Failed to load rate_limits.yml: %s", exc)
        return {}


def _parse_simple_yaml(path: Path) -> Dict[str, Any]:
    """Minimal YAML-subset parser for flat key-value and one level of nesting.

    Handles the structure we actually emit in the default config file without
    requiring PyYAML as a hard dependency.
    """
    result: Dict[str, Any] = {}
    current_section: Optional[str] = None
    current_dict: Dict[str, Any] = {}

    for raw_line in path.read_text().splitlines():
        # Strip comments
        line = raw_line.split("#")[0].rstrip()
        if not line or not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("-"):
            continue  # skip list items for now

        if ":" not in stripped:
            continue

        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()

        if indent == 0:
            # Top-level key
            if current_section and current_dict:
                result[current_section] = current_dict
                current_dict = {}
            if val:
                result[key] = _coerce_value(val)
                current_section = None
            else:
                current_section = key
                current_dict = {}
        elif indent >= 2 and current_section:
            if val:
                current_dict[key] = _coerce_value(val)
            else:
                # Nested dict (two levels deep) — store sub-dict
                current_dict[key] = {}

    if current_section and current_dict:
        result[current_section] = current_dict

    return result


def _coerce_value(val: str) -> Any:
    """Coerce a YAML scalar string to int, float, or str."""
    if not val:
        return val
    # Remove quotes
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    return val


class RateLimiter:
    """Per-tool sliding-window rate limiter with session cost tracking.

    Thread-safety: NOT thread-safe.  MCP servers are single-threaded per
    session, so this is fine.  If that changes, add a lock.
    """

    def __init__(self, config_path: Optional[Path] = None):
        # {tool_name: [timestamp, timestamp, ...]}  — sorted ascending
        self._calls: Dict[str, List[float]] = {}
        # {tool_name: float}  — cumulative cost per tool this session
        self._costs: Dict[str, float] = {}
        # Total session cost
        self._session_cost: float = 0.0
        # Session start time
        self._session_start: float = time.time()
        # Load config
        self._config = _load_config(config_path)
        self._custom_limits: Dict[str, int] = {}
        self._load_custom_limits()

    def _load_custom_limits(self) -> None:
        """Extract per-tool limit overrides from the config."""
        # Tier-level overrides
        tiers = self._config.get("tiers", {})
        if isinstance(tiers, dict):
            self._tier_overrides = {
                "free": int(tiers["free"]) if "free" in tiers else None,
                "pro": int(tiers["pro"]) if "pro" in tiers else None,
                "deliberation": int(tiers["deliberation"]) if "deliberation" in tiers else None,
            }
        else:
            self._tier_overrides = {}

        # Per-tool overrides
        tools = self._config.get("tools", {})
        if isinstance(tools, dict):
            for tool_name, settings in tools.items():
                if isinstance(settings, dict) and "limit" in settings:
                    self._custom_limits[tool_name] = int(settings["limit"])
                elif isinstance(settings, (int, float)):
                    self._custom_limits[tool_name] = int(settings)

    @property
    def session_cost_cap(self) -> float:
        """The maximum cost allowed per session."""
        cap = self._config.get("session_cost_cap")
        if cap is not None:
            try:
                return float(cap)
            except (TypeError, ValueError):
                pass
        return DEFAULT_SESSION_COST_CAP

    def _get_limit(self, tool_name: str) -> int:
        """Resolve the effective hourly limit for a tool."""
        # Per-tool override takes priority
        if tool_name in self._custom_limits:
            return self._custom_limits[tool_name]

        # Tier override
        tier = _classify_tool(tool_name)
        tier_override = getattr(self, "_tier_overrides", {}).get(tier)
        if tier_override is not None:
            return tier_override

        return _default_limit_for(tool_name)

    def _get_cost_estimate(self, tool_name: str) -> float:
        """Return the estimated cost per call for a tool."""
        # Config override
        tools = self._config.get("tools", {})
        if isinstance(tools, dict) and tool_name in tools:
            settings = tools[tool_name]
            if isinstance(settings, dict) and "cost" in settings:
                return float(settings["cost"])

        return DEFAULT_COST_ESTIMATES.get(tool_name, 0.0)

    def _prune_window(self, tool_name: str, now: float) -> List[float]:
        """Remove call timestamps outside the sliding window, return remaining."""
        if tool_name not in self._calls:
            return []
        cutoff = now - WINDOW_SECONDS
        calls = self._calls[tool_name]
        # Binary-ish prune: find first index >= cutoff
        pruned = [t for t in calls if t >= cutoff]
        self._calls[tool_name] = pruned
        return pruned

    def check(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Check if calling tool_name is allowed right now.

        Returns None if the call is permitted.
        Returns an error dict if the call should be blocked.
        """
        now = time.time()

        # 1. Session cost cap
        if self._session_cost >= self.session_cost_cap:
            return {
                "error": "session_cost_exceeded",
                "message": (
                    f"Session cost cap reached (${self._session_cost:.3f} / "
                    f"${self.session_cost_cap:.2f}). "
                    "To continue, increase the cap in ~/.delimit/rate_limits.yml "
                    "or call delimit_cost_controls to adjust."
                ),
                "session_cost": round(self._session_cost, 4),
                "session_cost_cap": self.session_cost_cap,
            }

        # 2. Per-tool rate limit
        recent = self._prune_window(tool_name, now)
        limit = self._get_limit(tool_name)
        count = len(recent)

        if count >= limit:
            # Calculate when the oldest call in the window will expire
            oldest = recent[0] if recent else now
            wait_seconds = int(oldest + WINDOW_SECONDS - now) + 1
            wait_minutes = max(1, wait_seconds // 60)
            tier = _classify_tool(tool_name)
            return {
                "error": "rate_limit_exceeded",
                "message": (
                    f"Rate limit exceeded for '{tool_name}': "
                    f"{count}/{limit} calls/hour ({tier} tier). "
                    f"Try again in ~{wait_minutes} minute(s), or increase the "
                    f"limit in ~/.delimit/rate_limits.yml"
                ),
                "tool": tool_name,
                "tier": tier,
                "calls_used": count,
                "calls_limit": limit,
                "retry_after_seconds": wait_seconds,
            }

        # 3. Prospective cost check — would this call push us over?
        estimated_cost = self._get_cost_estimate(tool_name)
        if estimated_cost > 0 and (self._session_cost + estimated_cost) > self.session_cost_cap:
            return {
                "error": "session_cost_would_exceed",
                "message": (
                    f"Executing '{tool_name}' (~${estimated_cost:.4f}) would "
                    f"exceed the session cost cap "
                    f"(${self._session_cost:.3f} + ${estimated_cost:.4f} > "
                    f"${self.session_cost_cap:.2f}). "
                    "Increase session_cost_cap in ~/.delimit/rate_limits.yml "
                    "or call delimit_cost_controls to adjust."
                ),
                "tool": tool_name,
                "estimated_cost": estimated_cost,
                "session_cost": round(self._session_cost, 4),
                "session_cost_cap": self.session_cost_cap,
            }

        # 4. Warning at 80% usage
        if count >= int(limit * WARNING_THRESHOLD) and count < limit:
            remaining = limit - count
            logger.warning(
                "Rate limit warning: '%s' at %d/%d calls/hour (%d remaining)",
                tool_name, count, limit, remaining,
            )

        return None  # Allowed

    def record(self, tool_name: str, cost: Optional[float] = None) -> None:
        """Record a tool call and its cost.

        Args:
            tool_name: The MCP tool that was called.
            cost: Actual cost in USD.  If None, uses the default estimate.
        """
        now = time.time()

        # Record timestamp
        if tool_name not in self._calls:
            self._calls[tool_name] = []
        self._calls[tool_name].append(now)

        # Record cost
        if cost is None:
            cost = self._get_cost_estimate(tool_name)
        if cost > 0:
            self._costs[tool_name] = self._costs.get(tool_name, 0.0) + cost
            self._session_cost += cost

        # Log periodic warnings
        recent = self._prune_window(tool_name, now)
        limit = self._get_limit(tool_name)
        if len(recent) == int(limit * WARNING_THRESHOLD):
            logger.warning(
                "Rate limit 80%% reached for '%s': %d/%d calls this hour",
                tool_name, len(recent), limit,
            )

    def get_usage(self) -> Dict[str, Any]:
        """Return current session usage summary.

        Returns a dict with:
            session_cost: total cost this session
            session_cost_cap: the configured cap
            session_duration_seconds: how long the session has been active
            tools: {tool_name: {calls_this_hour, limit, cost, tier, remaining}}
        """
        now = time.time()
        tools_usage: Dict[str, Dict[str, Any]] = {}

        # Collect all tools that have been called
        all_tools = set(self._calls.keys()) | set(self._costs.keys())

        for tool_name in sorted(all_tools):
            recent = self._prune_window(tool_name, now)
            limit = self._get_limit(tool_name)
            count = len(recent)
            tools_usage[tool_name] = {
                "calls_this_hour": count,
                "limit": limit,
                "remaining": max(0, limit - count),
                "cost_this_session": round(self._costs.get(tool_name, 0.0), 6),
                "cost_per_call": self._get_cost_estimate(tool_name),
                "tier": _classify_tool(tool_name),
            }

        return {
            "session_cost": round(self._session_cost, 4),
            "session_cost_cap": self.session_cost_cap,
            "session_cost_remaining": round(
                max(0, self.session_cost_cap - self._session_cost), 4
            ),
            "session_duration_seconds": int(now - self._session_start),
            "tools": tools_usage,
        }

    def get_quota(self, tool_name: str) -> Dict[str, Any]:
        """Return quota info for a single tool."""
        now = time.time()
        recent = self._prune_window(tool_name, now)
        limit = self._get_limit(tool_name)
        count = len(recent)
        return {
            "tool": tool_name,
            "tier": _classify_tool(tool_name),
            "calls_this_hour": count,
            "limit": limit,
            "remaining": max(0, limit - count),
            "cost_this_session": round(self._costs.get(tool_name, 0.0), 6),
            "cost_per_call": self._get_cost_estimate(tool_name),
        }

    def set_limit(self, tool_name: str, limit: int) -> None:
        """Override the hourly limit for a tool (session-scoped, not persisted)."""
        if limit < 0:
            raise ValueError("Limit must be non-negative")
        self._custom_limits[tool_name] = limit
        logger.info("Rate limit for '%s' set to %d calls/hour", tool_name, limit)

    def set_session_cost_cap(self, cap: float) -> None:
        """Override the session cost cap (session-scoped, not persisted)."""
        if cap < 0:
            raise ValueError("Cost cap must be non-negative")
        self._config["session_cost_cap"] = cap
        logger.info("Session cost cap set to $%.2f", cap)

    def reset(self) -> None:
        """Reset all tracking state. Starts a fresh session."""
        self._calls.clear()
        self._costs.clear()
        self._session_cost = 0.0
        self._session_start = time.time()
        logger.info("Rate limiter reset — new session started")

    def reset_tool(self, tool_name: str) -> None:
        """Reset tracking for a single tool."""
        self._calls.pop(tool_name, None)
        cost = self._costs.pop(tool_name, 0.0)
        self._session_cost = max(0, self._session_cost - cost)
        logger.info("Rate limiter reset for '%s'", tool_name)


# ---------------------------------------------------------------------------
#  Module-level singleton
# ---------------------------------------------------------------------------

limiter = RateLimiter()


def create_cost_controls_response(
    action: str = "status",
    tool_name: str = "",
    limit: Optional[int] = None,
    cost_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """Handler logic for the delimit_cost_controls MCP tool.

    Actions:
        status  — show full session usage
        quota   — show quota for a specific tool
        set     — set a custom limit for a tool or session cost cap
        reset   — reset all tracking
    """
    if action == "status":
        return {
            "status": "ok",
            **limiter.get_usage(),
            "hint": (
                "Use action='quota' with tool_name to check a specific tool, "
                "or action='set' to adjust limits."
            ),
        }

    if action == "quota":
        if not tool_name:
            return {"error": "tool_name is required for action='quota'"}
        return {"status": "ok", **limiter.get_quota(tool_name)}

    if action == "set":
        changes = []
        if tool_name and limit is not None:
            limiter.set_limit(tool_name, limit)
            changes.append(f"{tool_name} limit set to {limit}/hour")
        if cost_cap is not None:
            limiter.set_session_cost_cap(cost_cap)
            changes.append(f"session cost cap set to ${cost_cap:.2f}")
        if not changes:
            return {
                "error": "Provide tool_name+limit to set a tool limit, "
                "or cost_cap to set the session cost cap."
            }
        return {"status": "ok", "changes": changes}

    if action == "reset":
        limiter.reset()
        return {"status": "ok", "message": "All rate limit tracking reset."}

    return {
        "error": f"Unknown action '{action}'",
        "valid_actions": ["status", "quota", "set", "reset"],
    }
