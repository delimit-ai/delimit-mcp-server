"""
Delimit Governance Layer — the loop that keeps AI agents on track.

Every tool flows through governance. Governance:
1. Logs what happened (evidence)
2. Checks result against rules (thresholds, policies)
3. Auto-creates ledger items for failures/warnings
4. Suggests next steps (loops back to keep building)

This replaces _with_next_steps — governance IS the next step system.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.governance")


# Governance rules — what triggers auto-ledger-creation
RULES = {
    "test_coverage": {
        "threshold_key": "line_coverage",
        "threshold": 80,
        "comparison": "below",
        "ledger_title": "Test coverage below {threshold}% — currently {value}%",
        "ledger_type": "fix",
        "ledger_priority": "P1",
    },
    "security_audit": {
        "trigger_key": "vulnerabilities",
        "trigger_if_nonempty": True,
        "ledger_title": "Security: {count} vulnerabilities found",
        "ledger_type": "fix",
        "ledger_priority": "P0",
    },
    "security_scan": {
        "trigger_key": "vulnerabilities",
        "trigger_if_nonempty": True,
        "ledger_title": "Security scan: {count} issues detected",
        "ledger_type": "fix",
        "ledger_priority": "P0",
    },
    "lint": {
        "trigger_key": "violations",
        "trigger_if_nonempty": True,
        "ledger_title": "API lint: {count} violations found",
        "ledger_type": "fix",
        "ledger_priority": "P1",
    },
    "deliberate": {
        "trigger_key": "unanimous",
        "trigger_if_true": True,
        "extract_actions": True,
        "ledger_title": "Deliberation consensus reached — action items pending",
        "ledger_type": "strategy",
        "ledger_priority": "P1",
    },
    "gov_health": {
        "trigger_key": "status",
        "trigger_values": ["not_initialized", "degraded"],
        "ledger_title": "Governance health: {value} — needs attention",
        "ledger_type": "fix",
        "ledger_priority": "P1",
    },
    "docs_validate": {
        "threshold_key": "coverage_percent",
        "threshold": 50,
        "comparison": "below",
        "ledger_title": "Documentation coverage below {threshold}% — currently {value}%",
        "ledger_type": "task",
        "ledger_priority": "P2",
    },
}

# Next steps registry — what to do after each tool
NEXT_STEPS = {
    "lint": [
        {"tool": "delimit_explain", "reason": "Get migration guide for violations", "premium": False},
        {"tool": "delimit_semver", "reason": "Classify the version bump", "premium": False},
    ],
    "diff": [
        {"tool": "delimit_semver", "reason": "Classify changes as MAJOR/MINOR/PATCH", "premium": False},
        {"tool": "delimit_policy", "reason": "Check against governance policies", "premium": False},
    ],
    "semver": [
        {"tool": "delimit_explain", "reason": "Generate human-readable changelog", "premium": False},
    ],
    "init": [
        {"tool": "delimit_gov_health", "reason": "Verify governance is set up correctly", "premium": True},
        {"tool": "delimit_diagnose", "reason": "Check for any issues", "premium": False},
    ],
    "test_coverage": [
        {"tool": "delimit_test_generate", "reason": "Generate tests for uncovered files", "premium": False},
    ],
    "security_audit": [
        {"tool": "delimit_evidence_collect", "reason": "Collect evidence of findings", "premium": True},
    ],
    "gov_health": [
        {"tool": "delimit_gov_status", "reason": "See detailed governance status", "premium": True},
        {"tool": "delimit_repo_analyze", "reason": "Full repo health report", "premium": True},
    ],
    "deploy_plan": [
        {"tool": "delimit_deploy_build", "reason": "Build the deployment", "premium": True},
    ],
    "deploy_build": [
        {"tool": "delimit_deploy_publish", "reason": "Publish the build", "premium": True},
    ],
    "deploy_publish": [
        {"tool": "delimit_deploy_verify", "reason": "Verify the deployment", "premium": True},
    ],
    "deploy_verify": [
        {"tool": "delimit_deploy_rollback", "reason": "Rollback if unhealthy", "premium": True},
    ],
    "repo_analyze": [
        {"tool": "delimit_security_audit", "reason": "Scan for security issues", "premium": False},
        {"tool": "delimit_gov_health", "reason": "Check governance status", "premium": True},
    ],
    "deliberate": [
        {"tool": "delimit_ledger_context", "reason": "Review what's on the ledger after consensus", "premium": False},
    ],
    "ledger_add": [
        {"tool": "delimit_ledger_context", "reason": "See updated ledger state", "premium": False},
    ],
    "diagnose": [
        {"tool": "delimit_init", "reason": "Initialize governance if not set up", "premium": False},
    ],
}


def govern(tool_name: str, result: Dict[str, Any], project_path: str = ".") -> Dict[str, Any]:
    """
    Run governance on a tool's result. This is the central loop.

    1. Check result against rules
    2. Auto-create ledger items if thresholds breached
    3. Add next_steps for the AI to continue
    4. Return enriched result

    Every tool should call this before returning.
    """
    # Strip "delimit_" prefix for rule matching
    clean_name = tool_name.replace("delimit_", "")

    governed_result = dict(result)

    # 1. Check governance rules
    rule = RULES.get(clean_name)
    auto_items = []

    if rule:
        triggered = False
        context = {}

        # Threshold check (e.g., coverage < 80%)
        if "threshold_key" in rule:
            value = _deep_get(result, rule["threshold_key"])
            if value is not None:
                threshold = rule["threshold"]
                if rule.get("comparison") == "below" and value < threshold:
                    triggered = True
                    context = {"value": f"{value:.1f}" if isinstance(value, float) else str(value), "threshold": str(threshold)}

        # Non-empty list check (e.g., vulnerabilities found)
        if "trigger_key" in rule and "trigger_if_nonempty" in rule:
            items = _deep_get(result, rule["trigger_key"])
            if items and isinstance(items, list) and len(items) > 0:
                triggered = True
                context = {"count": str(len(items))}

        # Value match check (e.g., status == "degraded")
        if "trigger_key" in rule and "trigger_values" in rule:
            value = _deep_get(result, rule["trigger_key"])
            if value in rule["trigger_values"]:
                triggered = True
                context = {"value": str(value)}

        # Boolean check (e.g., unanimous == True)
        if "trigger_key" in rule and "trigger_if_true" in rule:
            value = _deep_get(result, rule["trigger_key"])
            if value:
                triggered = True

        if triggered:
            title = rule["ledger_title"].format(**context) if context else rule["ledger_title"]
            auto_items.append({
                "title": title,
                "type": rule.get("ledger_type", "task"),
                "priority": rule.get("ledger_priority", "P1"),
                "source": f"governance:{clean_name}",
            })

    # 2. Auto-create ledger items
    if auto_items:
        try:
            from ai.ledger_manager import add_item
            created = []
            for item in auto_items:
                entry = add_item(
                    title=item["title"],
                    type=item["type"],
                    priority=item["priority"],
                    source=item["source"],
                    project_path=project_path,
                )
                created.append(entry.get("added", {}).get("id", ""))
            governed_result["governance"] = {
                "action": "ledger_items_created",
                "items": created,
                "reason": "Governance rule triggered by tool result",
            }
        except Exception as e:
            logger.warning("Governance auto-ledger failed: %s", e)

    # 3. Add next steps
    steps = NEXT_STEPS.get(clean_name, [])
    if steps:
        governed_result["next_steps"] = steps

    # 4. Always suggest checking the ledger
    if clean_name not in ("ledger_add", "ledger_done", "ledger_list", "ledger_context", "ventures", "version", "help", "diagnose", "activate", "license_status", "models"):
        if "next_steps" not in governed_result:
            governed_result["next_steps"] = []
        # Don't duplicate
        existing = {s.get("tool") for s in governed_result.get("next_steps", [])}
        if "delimit_ledger_context" not in existing:
            governed_result["next_steps"].append({
                "tool": "delimit_ledger_context",
                "reason": "Check ledger for what's next",
                "premium": False,
            })
    else:
        # Excluded tools still get the next_steps field (empty) for schema consistency
        if "next_steps" not in governed_result:
            governed_result["next_steps"] = []

    return governed_result


def _deep_get(d: Dict, key: str) -> Any:
    """Get a value from a dict, supporting nested keys with dots."""
    if "." in key:
        parts = key.split(".", 1)
        sub = d.get(parts[0])
        if isinstance(sub, dict):
            return _deep_get(sub, parts[1])
        return None

    # Check top-level and common nested locations
    if key in d:
        return d[key]
    # Check inside 'data', 'result', 'overall_coverage'
    for wrapper in ["data", "result", "overall_coverage", "summary"]:
        if isinstance(d.get(wrapper), dict) and key in d[wrapper]:
            return d[wrapper][key]
    return None
