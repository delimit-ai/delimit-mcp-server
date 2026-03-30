"""Agent Swarm — persona registry, namespace isolation, and venture management.

Implements Agent Swarm Standard v1.2 (4-party consent achieved 2026-03-30).
Each venture gets 5 agent roles bound to AI models with namespace isolation.

Config: ~/.delimit/swarm/config.yml
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SWARM_DIR = Path.home() / ".delimit" / "swarm"
REGISTRY_FILE = SWARM_DIR / "agent_registry.json"
VENTURES_FILE = SWARM_DIR / "ventures.json"
SWARM_LOG = SWARM_DIR / "swarm_log.jsonl"

# Default roster from Agent Swarm Standard v1.2
DEFAULT_ROSTER = {
    "architect": {
        "role": "System Design, Architecture, Complex Problem Solving",
        "default_model": "claude-opus-4.6",
        "fallback_model": "grok-4",
    },
    "senior_dev": {
        "role": "Implementation, Code Generation, Feature Building",
        "default_model": "claude-opus-4.6",
        "fallback_model": "codex-gpt-5.4",
    },
    "reviewer": {
        "role": "Code Review, PR Analysis, Bug Detection",
        "default_model": "gemini-3.1-pro-preview",
        "fallback_model": "grok-4",
    },
    "qa": {
        "role": "Quality Assurance, Testing, CI Verification",
        "default_model": "gemini-3.1-pro-preview",
        "fallback_model": "codex-gpt-5.4",
    },
    "ops": {
        "role": "Strategy, Deliberation, Outreach, Competitive Intel",
        "default_model": "grok-4",
        "fallback_model": "gemini-3.1-pro-preview",
    },
}


def _ensure_dir():
    SWARM_DIR.mkdir(parents=True, exist_ok=True)


def _load_registry() -> Dict[str, Any]:
    if not REGISTRY_FILE.exists():
        return {"agents": {}, "version": "1.2"}
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"agents": {}, "version": "1.2"}


def _save_registry(registry: Dict[str, Any]):
    _ensure_dir()
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def _load_ventures() -> Dict[str, Any]:
    if not VENTURES_FILE.exists():
        return {}
    try:
        return json.loads(VENTURES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_ventures(ventures: Dict[str, Any]):
    _ensure_dir()
    VENTURES_FILE.write_text(json.dumps(ventures, indent=2))


def _log(entry: Dict[str, Any]):
    _ensure_dir()
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(SWARM_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def register_venture(
    name: str,
    namespace: str = "",
    repo_path: str = "",
    deploy_target: str = "",
    custom_tools: Optional[List[str]] = None,
    special_rules: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Register a venture in the swarm with its own namespace and agent team."""
    if not name:
        return {"error": "name is required"}

    name = name.strip().lower()
    namespace = namespace or name.replace(" ", "_").replace("-", "_")

    ventures = _load_ventures()
    ventures[name] = {
        "name": name,
        "namespace": namespace,
        "repo_path": repo_path,
        "deploy_target": deploy_target,
        "custom_tools": custom_tools or [],
        "special_rules": special_rules or [],
        "created_at": ventures.get(name, {}).get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_ventures(ventures)

    # Auto-create agents for this venture
    registry = _load_registry()
    agents_created = 0
    for role_key, role_config in DEFAULT_ROSTER.items():
        agent_id = f"{namespace}-{role_key}-01"
        if agent_id not in registry["agents"]:
            registry["agents"][agent_id] = {
                "id": agent_id,
                "venture": name,
                "namespace": namespace,
                "role": role_key,
                "role_description": role_config["role"],
                "model": role_config["default_model"],
                "fallback_model": role_config["fallback_model"],
                "permissions": {
                    "read": f"{namespace}/*",
                    "write": f"{namespace}/src/*",
                    "deploy": False,
                },
                "status": "active",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            agents_created += 1

    _save_registry(registry)
    _log({"action": "register_venture", "venture": name, "agents_created": agents_created})

    return {
        "status": "registered",
        "venture": name,
        "namespace": namespace,
        "agents_created": agents_created,
        "total_agents": len([a for a in registry["agents"].values() if a["venture"] == name]),
        "message": f"Venture '{name}' registered with {agents_created} new agent(s)",
    }


def get_venture(name: str = "") -> Dict[str, Any]:
    """Get venture details, or list all ventures."""
    ventures = _load_ventures()

    if not name:
        return {
            "status": "ok",
            "ventures": list(ventures.values()),
            "total": len(ventures),
        }

    name = name.strip().lower()
    if name not in ventures:
        return {"error": f"Venture '{name}' not found"}

    venture = ventures[name]
    registry = _load_registry()
    agents = [a for a in registry["agents"].values() if a["venture"] == name]

    return {
        "status": "ok",
        "venture": venture,
        "agents": agents,
        "agent_count": len(agents),
    }


def get_agent(agent_id: str = "") -> Dict[str, Any]:
    """Get agent details, or list all agents across ventures."""
    registry = _load_registry()

    if not agent_id:
        agents = list(registry["agents"].values())
        by_venture = {}
        for a in agents:
            by_venture.setdefault(a["venture"], []).append({
                "id": a["id"],
                "role": a["role"],
                "model": a["model"],
                "status": a["status"],
            })
        return {
            "status": "ok",
            "total_agents": len(agents),
            "by_venture": by_venture,
        }

    agent = registry["agents"].get(agent_id)
    if not agent:
        return {"error": f"Agent '{agent_id}' not found"}
    return {"status": "ok", "agent": agent}


def check_namespace_access(
    agent_id: str,
    target_path: str,
    action: str = "read",
) -> Dict[str, Any]:
    """Check if an agent has access to a path within namespace isolation rules."""
    registry = _load_registry()
    agent = registry["agents"].get(agent_id)

    if not agent:
        return {"allowed": False, "reason": f"Agent '{agent_id}' not found"}

    namespace = agent["namespace"]
    ventures = _load_ventures()
    venture = ventures.get(agent["venture"], {})
    repo_path = venture.get("repo_path", "")

    # Check if target is within the venture's namespace
    if repo_path and target_path.startswith(repo_path):
        if action == "read":
            return {"allowed": True, "agent": agent_id, "reason": "Within venture namespace"}
        if action == "write":
            return {"allowed": True, "agent": agent_id, "reason": "Write within namespace"}
        if action == "deploy":
            if agent["permissions"].get("deploy", False):
                return {"allowed": True, "agent": agent_id, "reason": "Deploy permitted"}
            return {"allowed": False, "agent": agent_id, "reason": "Deploy requires founder approval"}

    # Cross-venture access blocked by default
    return {
        "allowed": False,
        "agent": agent_id,
        "target": target_path,
        "namespace": namespace,
        "reason": f"Cross-venture access blocked. Agent '{agent_id}' can only access {namespace}/* paths.",
    }


def get_swarm_status() -> Dict[str, Any]:
    """Get the full swarm status — ventures, agents, health."""
    ventures = _load_ventures()
    registry = _load_registry()
    agents = list(registry["agents"].values())

    return {
        "status": "ok",
        "version": registry.get("version", "1.2"),
        "ventures": len(ventures),
        "total_agents": len(agents),
        "active_agents": len([a for a in agents if a["status"] == "active"]),
        "by_venture": {
            v: {
                "agents": len([a for a in agents if a["venture"] == v]),
                "namespace": ventures[v].get("namespace", v),
                "repo": ventures[v].get("repo_path", ""),
            }
            for v in ventures
        },
        "roster": list(DEFAULT_ROSTER.keys()),
    }


# ═══════════════════════════════════════════════════════════════════════
#  LED-276: Central Governor — tiered approvals + auto-escalation
# ═══════════════════════════════════════════════════════════════════════

APPROVAL_TIERS = {
    "deploy_production": "founder_required",
    "deploy_staging": "auto_approved",
    "social_post": "founder_email",
    "social_low_risk": "auto_after_consensus",
    "outreach_issue": "founder_email",
    "ledger_update": "auto_approved",
    "code_commit": "auto_approved",
    "security_audit": "auto_approved",
}

ESCALATION_RULES = [
    {"trigger": "collision_detected", "action": "halt_and_notify", "severity": "high"},
    {"trigger": "prompt_drift_exceeded", "action": "pause_agent", "severity": "medium"},
    {"trigger": "unauthorized_deploy", "action": "block_and_alert", "severity": "critical"},
    {"trigger": "pii_detected_outbound", "action": "redact_and_log", "severity": "high"},
    {"trigger": "xai_credits_exhausted", "action": "pause_posting", "severity": "medium"},
    {"trigger": "model_error_rate_high", "action": "switch_to_fallback", "severity": "medium"},
]


def check_approval(action: str, venture: str = "", agent_id: str = "") -> Dict[str, Any]:
    """Check if an action requires approval or is auto-approved.

    Tiered approval system:
    - deploy_production: always requires founder approval
    - deploy_staging: auto-approved
    - social_post: founder email approval
    - social_low_risk: auto after multi-model consensus
    - code_commit, ledger_update: auto-approved
    """
    tier = APPROVAL_TIERS.get(action, "founder_required")

    result = {
        "action": action,
        "tier": tier,
        "venture": venture,
        "agent_id": agent_id,
    }

    if tier == "auto_approved":
        result["approved"] = True
        result["message"] = f"'{action}' is auto-approved"
    elif tier == "auto_after_consensus":
        result["approved"] = False
        result["message"] = f"'{action}' requires multi-model consensus before auto-approval"
        result["next_step"] = "Run delimit_deliberate on the proposed action"
    elif tier == "founder_email":
        result["approved"] = False
        result["message"] = f"'{action}' requires founder email approval"
        result["next_step"] = "Send via delimit_notify for founder review"
    else:
        result["approved"] = False
        result["message"] = f"'{action}' requires founder approval"
        result["next_step"] = "Submit for founder review via dashboard or email"

    _log({"action": "approval_check", "requested": action, "result": tier, "venture": venture, "agent": agent_id})
    return result


def get_escalation_rules() -> Dict[str, Any]:
    """Get the current escalation rules for the central governor."""
    return {
        "status": "ok",
        "rules": ESCALATION_RULES,
        "approval_tiers": APPROVAL_TIERS,
    }


# ═══════════════════════════════════════════════════════════════════════
#  Usage Guide
# ═══════════════════════════════════════════════════════════════════════

USAGE_GUIDE = """
# Delimit Agent Swarm — Usage Guide

## Quick Start

1. Register your ventures:
   delimit_swarm(action="register", venture="my-project", repo_path="/path/to/repo")

2. View your swarm:
   delimit_swarm(action="status")

3. Check agent permissions:
   delimit_swarm(action="check", agent_id="my_project-architect-01", target_path="/path/to/file")

4. Check approval tier:
   delimit_swarm(action="approve", action_name="deploy_production")

## Agent Roles

Each venture gets 5 agent roles:
- architect: System design (default: Claude Opus)
- senior_dev: Implementation (default: Claude Opus)
- reviewer: Code review (default: Gemini)
- qa: Testing (default: Gemini)
- ops: Strategy & outreach (default: Grok)

## Namespace Isolation

- Agents can only access files within their venture's repo path
- Cross-venture access is blocked by default
- Deploy requires founder approval (auto for staging)
- All actions are logged to ~/.delimit/swarm/swarm_log.jsonl

## Approval Tiers

| Action | Tier |
|--------|------|
| deploy_production | Founder approval required |
| deploy_staging | Auto-approved |
| social_post | Founder email approval |
| code_commit | Auto-approved |
| ledger_update | Auto-approved |

## Escalation

Critical alerts auto-escalate: collision, unauthorized deploy, PII detected.
Medium alerts: prompt drift, model errors, credit exhaustion.
"""


def get_usage_guide() -> Dict[str, Any]:
    """Get the swarm usage guide."""
    return {"guide": USAGE_GUIDE, "version": "1.2"}


# ═══════════════════════════════════════════════════════════════════════
#  LED-278: GTM Tracking — speed, revenue, venture launches
# ═══════════════════════════════════════════════════════════════════════

METRICS_FILE = SWARM_DIR / "metrics.json"


def _load_metrics() -> Dict[str, Any]:
    if not METRICS_FILE.exists():
        return {"daily": {}, "ventures": {}}
    try:
        return json.loads(METRICS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"daily": {}, "ventures": {}}


def _save_metrics(metrics: Dict[str, Any]):
    _ensure_dir()
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))


def record_metric(
    venture: str,
    metric_type: str,
    value: float = 1.0,
    note: str = "",
) -> Dict[str, Any]:
    """Record a GTM metric for tracking swarm performance.

    Metric types: tasks_completed, deploy_count, revenue, launch_event,
                  outreach_sent, user_signup, feature_shipped.
    """
    if not venture or not metric_type:
        return {"error": "venture and metric_type required"}

    metrics = _load_metrics()
    today = time.strftime("%Y-%m-%d")

    # Daily tracking
    if today not in metrics["daily"]:
        metrics["daily"][today] = {}
    day = metrics["daily"][today]
    key = f"{venture}:{metric_type}"
    day[key] = day.get(key, 0) + value

    # Venture totals
    if venture not in metrics["ventures"]:
        metrics["ventures"][venture] = {}
    vtotals = metrics["ventures"][venture]
    vtotals[metric_type] = vtotals.get(metric_type, 0) + value

    _save_metrics(metrics)
    _log({"action": "metric", "venture": venture, "type": metric_type, "value": value, "note": note})

    return {
        "status": "recorded",
        "venture": venture,
        "metric": metric_type,
        "value": value,
        "today_total": day[key],
        "all_time": vtotals[metric_type],
    }


def get_metrics(venture: str = "", days: int = 7) -> Dict[str, Any]:
    """Get GTM metrics — speed, revenue, launches across ventures.

    Shows daily breakdown and venture totals. Used for dogfood tracking
    and build-in-public content.
    """
    metrics = _load_metrics()

    # Filter by date range
    today = time.strftime("%Y-%m-%d")
    recent_days = {}
    for date_str, day_data in sorted(metrics["daily"].items(), reverse=True)[:days]:
        if venture:
            filtered = {k: v for k, v in day_data.items() if k.startswith(f"{venture}:")}
            if filtered:
                recent_days[date_str] = filtered
        else:
            recent_days[date_str] = day_data

    # Venture totals
    if venture:
        vtotals = metrics["ventures"].get(venture, {})
    else:
        vtotals = metrics["ventures"]

    return {
        "status": "ok",
        "daily": recent_days,
        "totals": vtotals,
        "venture_filter": venture or "all",
        "days": days,
    }
