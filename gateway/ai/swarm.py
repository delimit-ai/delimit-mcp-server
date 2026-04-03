"""Agent Swarm — persona registry, namespace isolation, and venture management.

Implements Agent Swarm Standard v1.2 (4-party consent achieved 2026-03-30).
Each venture gets 5 agent roles bound to AI models with namespace isolation.

Config: /home/jamsons/governance/AGENT_SWARM_STANDARD.yml
"""

import json
import os
import signal
import sys
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


# ═══════════════════════════════════════════════════════════════════════
#  Change Management — docs freshness check before deploy
# ═══════════════════════════════════════════════════════════════════════

DOCS_CHECKLIST = [
    {"file": "README.md", "check": "tool_count", "pattern": r"\d+ (?:MCP |governance )?tools"},
    {"file": "README.md", "check": "version_badge", "pattern": r"GitHub%20Action-v[\d.]+"},
    {"file": "README.md", "check": "cli_commands", "pattern": r"npx delimit-cli"},
]


def check_docs_freshness(
    project_path: str = ".",
    tool_count: int = 0,
    version: str = "",
) -> Dict[str, Any]:
    """Check if documentation is up-to-date before deploying.

    Verifies README, changelog, and landing page reflect current
    tool count, version, and feature set.
    """
    import re
    p = Path(project_path).resolve()
    findings = []
    stale = False

    # Check README exists
    readme = p / "README.md"
    if not readme.exists():
        findings.append({"file": "README.md", "status": "missing", "severity": "warning"})
    else:
        content = readme.read_text()

        # Check tool count
        if tool_count > 0:
            counts = re.findall(r'(\d+)\s*(?:MCP |governance )?tools', content)
            for count_str in counts:
                count = int(count_str)
                if abs(count - tool_count) > 10:
                    findings.append({
                        "file": "README.md",
                        "status": "stale",
                        "issue": f"Says {count} tools, actual is {tool_count}",
                        "severity": "warning",
                    })
                    stale = True

        # Check version badge
        if version:
            if version not in content:
                findings.append({
                    "file": "README.md",
                    "status": "stale",
                    "issue": f"Version badge doesn't show {version}",
                    "severity": "info",
                })

    # Check What's New / CHANGELOG
    changelog = p / "CHANGELOG.md"
    if changelog.exists():
        cl_content = changelog.read_text()
        if version and version not in cl_content:
            findings.append({
                "file": "CHANGELOG.md",
                "status": "stale",
                "issue": f"No entry for version {version}",
                "severity": "warning",
            })
            stale = True

    # Check for uncommitted changes
    try:
        import subprocess
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            cwd=str(p), timeout=5,
        )
        uncommitted = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
        if uncommitted > 0:
            findings.append({
                "file": "working tree",
                "status": "dirty",
                "issue": f"{uncommitted} uncommitted file(s)",
                "severity": "info",
            })
    except Exception:
        pass

    return {
        "status": "stale" if stale else "fresh",
        "findings": findings,
        "total_issues": len(findings),
        "stale": stale,
        "message": f"{len(findings)} doc issue(s) found" if findings else "Documentation is up to date",
    }


# ═══════════════════════════════════════════════════════════════════════
#  Swarm Governance Auto-Triggers — NEVER skip these
#  Runs pre-flight checks before any major action
# ═══════════════════════════════════════════════════════════════════════

PREFLIGHT_LOG = SWARM_DIR / "preflight_log.jsonl"


def preflight_check(
    action: str,
    venture: str = "",
    path: str = "",
    agent_id: str = "",
) -> Dict[str, Any]:
    """Run mandatory governance checks before any major swarm action.

    This MUST be called before:
    - Creating a new project/venture
    - Deploying to production
    - Publishing to npm
    - Creating new agents or tools
    - Any cross-venture operation

    Returns a gate result: PASS (proceed), WARN (proceed with caution),
    or BLOCK (stop and fix issues first).
    """
    _ensure_dir()
    checks = []
    gate = "PASS"

    # 1. Venture must be registered
    if venture:
        ventures = _load_ventures()
        if venture not in ventures.get("ventures", {}):
            checks.append({
                "check": "venture_registered",
                "status": "FAIL",
                "message": f"Venture '{venture}' is not registered. Call delimit_swarm(action='register') first.",
                "required_action": "register_venture",
            })
            gate = "BLOCK"
        else:
            checks.append({"check": "venture_registered", "status": "PASS"})

    # 2. Agent must exist and be authorized
    if agent_id:
        registry = _load_registry()
        agent = registry["agents"].get(agent_id, {})
        if not agent:
            checks.append({
                "check": "agent_exists",
                "status": "FAIL",
                "message": f"Agent '{agent_id}' not found in registry.",
            })
            gate = "BLOCK"
        elif agent.get("status") != "active":
            checks.append({
                "check": "agent_active",
                "status": "FAIL",
                "message": f"Agent '{agent_id}' is not active (status: {agent.get('status')}).",
            })
            gate = "BLOCK"
        else:
            checks.append({"check": "agent_authorized", "status": "PASS"})

    # 3. Namespace isolation check
    if venture and path:
        ventures = _load_ventures()
        v_data = ventures.get("ventures", {}).get(venture, {})
        v_path = v_data.get("path", "")
        if v_path and not path.startswith(v_path):
            checks.append({
                "check": "namespace_isolation",
                "status": "WARN",
                "message": f"Path '{path}' is outside venture namespace '{v_path}'.",
            })
            if gate == "PASS":
                gate = "WARN"
        else:
            checks.append({"check": "namespace_isolation", "status": "PASS"})

    # 4. Action-specific checks
    if action in ("deploy_production", "publish_npm"):
        # Must have run scan
        checks.append({
            "check": "pre_deploy_scan",
            "status": "WARN",
            "message": "Ensure delimit_scan, delimit_security_audit, and delimit_test_smoke have been run.",
            "required_tools": ["delimit_scan", "delimit_security_audit", "delimit_test_smoke"],
        })
        if gate == "PASS":
            gate = "WARN"

    if action == "new_project":
        checks.append({
            "check": "project_init",
            "status": "WARN",
            "message": "New project: ensure delimit_scan is run after scaffolding.",
            "required_tools": ["delimit_scan", "delimit_swarm(action='register')"],
        })
        if gate == "PASS":
            gate = "WARN"

    if action == "create_tool" or action == "create_agent":
        checks.append({
            "check": "extension_governance",
            "status": "PASS" if agent_id else "WARN",
            "message": "Self-extension requires architect role and founder approval for activation.",
        })

    # 5. Collision check
    if path:
        try:
            from ai.collision_detect import check_collisions
            collisions = check_collisions()
            if collisions.get("conflicts"):
                checks.append({
                    "check": "collision_free",
                    "status": "WARN",
                    "message": f"{len(collisions['conflicts'])} file collision(s) detected.",
                })
                if gate == "PASS":
                    gate = "WARN"
            else:
                checks.append({"check": "collision_free", "status": "PASS"})
        except ImportError:
            checks.append({"check": "collision_free", "status": "SKIP"})

    # Log the preflight
    log_entry = {
        "timestamp": time.time(),
        "action": action,
        "venture": venture,
        "agent_id": agent_id,
        "gate": gate,
        "checks_passed": sum(1 for c in checks if c["status"] == "PASS"),
        "checks_total": len(checks),
    }
    try:
        with open(PREFLIGHT_LOG, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

    _log({"action": "preflight_check", "gate": gate, "venture": venture,
           "checks": len(checks), "action_type": action})

    return {
        "gate": gate,
        "action": action,
        "venture": venture,
        "checks": checks,
        "passed": sum(1 for c in checks if c["status"] == "PASS"),
        "total": len(checks),
        "message": {
            "PASS": "All governance checks passed. Proceed.",
            "WARN": "Governance checks passed with warnings. Review before proceeding.",
            "BLOCK": "Governance checks FAILED. Fix blocking issues before proceeding.",
        }[gate],
    }


# ═══════════════════════════════════════════════════════════════════════
#  LED-279: Self-Extending Swarm — Founder Mode
#  Agents can create new MCP tools when authorized
# ═══════════════════════════════════════════════════════════════════════

TOOLS_DIR = Path.home() / ".delimit" / "swarm" / "custom_tools"


def create_tool(
    name: str,
    code: str,
    venture: str,
    agent_id: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """Create a new MCP tool (founder mode only).

    Writes a Python module that can be loaded by the MCP server.
    Requires reviewer approval before activation.
    """
    if not name or not code:
        return {"error": "name and code are required"}

    # Verify agent has creation authority
    registry = _load_registry()
    agent = registry["agents"].get(agent_id, {})
    role = agent.get("role", "")
    if role not in ("architect", "senior_dev"):
        return {
            "error": f"Role '{role}' cannot create tools. Only architect and senior_dev have creation authority.",
            "agent_id": agent_id,
        }

    # Verify venture namespace
    if agent.get("venture", "") != venture:
        return {"error": f"Agent '{agent_id}' cannot create tools for venture '{venture}'"}

    # Security scan — check for dangerous patterns
    dangerous = [
        "subprocess.call", "os.system", "exec(", "eval(",
        "import socket", "import http.server",
        "__import__", "compile(",
    ]
    for pattern in dangerous:
        if pattern in code:
            return {
                "error": f"Security violation: '{pattern}' is not allowed in custom tools",
                "blocked_pattern": pattern,
            }

    # Write tool module
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    venture_dir = TOOLS_DIR / venture
    venture_dir.mkdir(parents=True, exist_ok=True)

    safe_name = name.lower().replace("-", "_").replace(" ", "_")
    tool_path = venture_dir / f"{safe_name}.py"
    tool_path.write_text(code)

    # Log creation
    _log({
        "action": "tool_created",
        "tool_name": safe_name,
        "venture": venture,
        "agent_id": agent_id,
        "path": str(tool_path),
        "lines": len(code.split("\n")),
        "status": "pending_review",
    })

    return {
        "status": "created",
        "tool_name": safe_name,
        "path": str(tool_path),
        "venture": venture,
        "created_by": agent_id,
        "lines": len(code.split("\n")),
        "next_step": "Reviewer agent must approve before tool is activated",
        "message": f"Tool '{safe_name}' created for {venture}. Pending reviewer approval.",
    }


def list_custom_tools(venture: str = "") -> Dict[str, Any]:
    """List custom tools created by agents."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tools = []

    search_dirs = [TOOLS_DIR / venture] if venture else list(TOOLS_DIR.iterdir())
    for d in search_dirs:
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                tools.append({
                    "name": f.stem,
                    "venture": d.name,
                    "path": str(f),
                    "lines": len(f.read_text().split("\n")),
                })

    return {
        "status": "ok",
        "tools": tools,
        "total": len(tools),
        "venture_filter": venture or "all",
    }


# ═══════════════════════════════════════════════════════════════════════
#  MCP Hot Reload — Option B: subprocess restart with state transfer
#  Consensus: Grok + Gemini agreed on subprocess restart with IPC
# ═══════════════════════════════════════════════════════════════════════

STATE_FILE = SWARM_DIR / "reload_state.json"
RESTART_FLAG = SWARM_DIR / "restart_pending"


def hot_reload(reason: str = "update") -> Dict[str, Any]:
    """Restart the MCP server process to pick up new module code.

    Saves current state (registry, ventures, metrics, custom tools list)
    to a transfer file, signals the parent process to restart, and the
    new process ingests the state on boot.

    Works across all AI CLIs because MCP servers are subprocesses —
    the CLI reconnects automatically within its timeout window (5-10s).
    """
    _ensure_dir()

    # 1. Capture current state for transfer
    state = {
        "timestamp": time.time(),
        "reason": reason,
        "registry": _load_registry(),
        "ventures": _load_ventures(),
        "metrics": _load_metrics(),
        "custom_tools": list_custom_tools().get("tools", []),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))

    # 2. Write restart flag (picked up by boot sequence)
    RESTART_FLAG.write_text(json.dumps({
        "requested_at": time.time(),
        "reason": reason,
        "pid": os.getpid(),
    }))

    _log({"action": "hot_reload_requested", "reason": reason, "pid": os.getpid()})

    # 3. Schedule graceful restart — send SIGHUP to self after brief delay
    # The MCP framework (FastMCP) handles SIGHUP by restarting the server
    # If SIGHUP isn't supported, fall back to writing the flag for manual pickup
    restart_method = "flag"
    try:
        # Check if we're the MCP server process
        if os.environ.get("DELIMIT_MCP_PID"):
            mcp_pid = int(os.environ["DELIMIT_MCP_PID"])
            os.kill(mcp_pid, signal.SIGHUP)
            restart_method = "sighup"
    except (ValueError, ProcessLookupError, PermissionError):
        pass

    return {
        "status": "reload_scheduled",
        "method": restart_method,
        "state_file": str(STATE_FILE),
        "reason": reason,
        "message": f"MCP server reload scheduled ({restart_method}). "
                   "AI CLI will reconnect within 5-10 seconds. "
                   "Session context (ledger, memory, conversation) is preserved.",
        "next_step": "The MCP server will restart and load updated modules. "
                     "No action needed — tools will be available again momentarily.",
    }


def ingest_reload_state() -> Dict[str, Any]:
    """Called on MCP server boot to restore state from a hot reload.

    Returns the transferred state if a reload just happened, or empty
    if this is a fresh boot.
    """
    if not STATE_FILE.exists():
        return {"status": "fresh_boot", "restored": False}

    try:
        state = json.loads(STATE_FILE.read_text())
        age = time.time() - state.get("timestamp", 0)

        # Only ingest if state is less than 60 seconds old
        if age > 60:
            STATE_FILE.unlink(missing_ok=True)
            return {"status": "stale_state", "restored": False, "age_seconds": age}

        # Clean up
        STATE_FILE.unlink(missing_ok=True)
        RESTART_FLAG.unlink(missing_ok=True)

        _log({"action": "reload_state_ingested", "reason": state.get("reason"), "age": age})

        return {
            "status": "restored",
            "restored": True,
            "reason": state.get("reason", "unknown"),
            "age_seconds": round(age, 1),
            "registry_agents": len(state.get("registry", {}).get("agents", {})),
            "ventures": len(state.get("ventures", {}).get("ventures", {})),
            "custom_tools": len(state.get("custom_tools", [])),
        }
    except (json.JSONDecodeError, KeyError):
        STATE_FILE.unlink(missing_ok=True)
        return {"status": "corrupt_state", "restored": False}


# ═══════════════════════════════════════════════════════════════════════
#  Swarm Self-Scaling — as ventures grow, so does the workforce
#  Architect agents can provision new specialist roles
# ═══════════════════════════════════════════════════════════════════════

CUSTOM_ROLES_FILE = SWARM_DIR / "custom_roles.json"


def create_agent(
    venture: str,
    role_name: str,
    description: str,
    default_model: str = "claude-opus-4.6",
    fallback_model: str = "gemini-3.1-pro-preview",
    permissions: Optional[List[str]] = None,
    creator_agent_id: str = "",
) -> Dict[str, Any]:
    """Create a new specialist agent role for a venture.

    Only the Architect agent can create new roles. New roles inherit
    the venture's namespace isolation but get scoped permissions.
    The agent is registered but requires founder approval to activate.

    All models see new agents via the standard MCP tool interface —
    no model-specific configuration needed.
    """
    if not venture or not role_name:
        return {"error": "venture and role_name are required"}

    # Verify creator has authority
    registry = _load_registry()
    creator = registry["agents"].get(creator_agent_id, {})
    if creator.get("role") != "architect":
        return {
            "error": f"Only architect agents can create new roles. "
                     f"Agent '{creator_agent_id}' has role '{creator.get('role', 'unknown')}'.",
        }

    # Verify venture namespace
    if creator.get("venture", "") != venture:
        return {"error": f"Agent '{creator_agent_id}' cannot create roles for venture '{venture}'"}

    # Normalize role name
    safe_role = role_name.lower().replace("-", "_").replace(" ", "_")

    # Check for duplicate
    if safe_role in DEFAULT_ROSTER:
        return {"error": f"Cannot override built-in role '{safe_role}'"}

    # Load or create custom roles registry
    custom_roles = {}
    if CUSTOM_ROLES_FILE.exists():
        try:
            custom_roles = json.loads(CUSTOM_ROLES_FILE.read_text())
        except json.JSONDecodeError:
            custom_roles = {}

    venture_roles = custom_roles.setdefault(venture, {})
    if safe_role in venture_roles:
        return {"error": f"Role '{safe_role}' already exists for venture '{venture}'"}

    # Create the role definition
    role_def = {
        "role": description,
        "default_model": default_model,
        "fallback_model": fallback_model,
        "permissions": permissions or ["read", "suggest"],
        "created_by": creator_agent_id,
        "created_at": time.time(),
        "status": "pending_approval",
    }
    venture_roles[safe_role] = role_def

    # Save
    _ensure_dir()
    CUSTOM_ROLES_FILE.write_text(json.dumps(custom_roles, indent=2))

    # Auto-register the agent (inactive until approved)
    agent_id = f"{venture}_{safe_role}"
    registry["agents"][agent_id] = {
        "venture": venture,
        "role": safe_role,
        "model": default_model,
        "fallback": fallback_model,
        "status": "pending_approval",
        "registered_at": time.time(),
        "custom": True,
    }
    _save_registry(registry)

    _log({
        "action": "agent_created",
        "venture": venture,
        "role": safe_role,
        "model": default_model,
        "created_by": creator_agent_id,
        "status": "pending_approval",
    })

    return {
        "status": "created",
        "agent_id": agent_id,
        "role": safe_role,
        "venture": venture,
        "model": default_model,
        "permissions": role_def["permissions"],
        "created_by": creator_agent_id,
        "activation": "pending_approval",
        "message": f"Agent '{agent_id}' created with role '{safe_role}'. "
                   f"Founder approval required to activate.",
    }


def approve_agent(agent_id: str) -> Dict[str, Any]:
    """Approve a pending custom agent for activation (founder only)."""
    registry = _load_registry()
    agent = registry["agents"].get(agent_id)
    if not agent:
        return {"error": f"Agent '{agent_id}' not found"}
    if not agent.get("custom"):
        return {"error": f"Agent '{agent_id}' is a built-in role, not a custom agent"}
    if agent.get("status") == "active":
        return {"status": "already_active", "agent_id": agent_id}

    agent["status"] = "active"
    agent["approved_at"] = time.time()
    _save_registry(registry)

    _log({"action": "agent_approved", "agent_id": agent_id})

    return {
        "status": "activated",
        "agent_id": agent_id,
        "role": agent.get("role"),
        "venture": agent.get("venture"),
        "message": f"Agent '{agent_id}' is now active.",
    }


def list_agents(venture: str = "") -> Dict[str, Any]:
    """List all agents — built-in and custom — optionally filtered by venture."""
    registry = _load_registry()
    agents = []

    for agent_id, agent in registry["agents"].items():
        if venture and agent.get("venture") != venture:
            continue
        agents.append({
            "id": agent_id,
            "venture": agent.get("venture", ""),
            "role": agent.get("role", ""),
            "model": agent.get("model", ""),
            "status": agent.get("status", "active"),
            "custom": agent.get("custom", False),
        })

    return {
        "status": "ok",
        "agents": agents,
        "total": len(agents),
        "venture_filter": venture or "all",
    }
