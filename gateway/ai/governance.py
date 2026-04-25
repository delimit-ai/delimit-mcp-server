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
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _is_test_mode() -> bool:
    """Return True when DELIMIT_TEST_MODE is explicitly set.

    When True the governance loop skips real ledger writes to avoid
    polluting the project ledger with mock/test data.

    We intentionally do NOT auto-detect PYTEST_CURRENT_TEST here
    because the gateway's own test suite mocks ledger calls and needs
    governance to attempt those calls so assertions work.  Set
    DELIMIT_TEST_MODE=1 in external test harnesses that trigger
    governance but do not mock the ledger.
    """
    return bool(os.environ.get("DELIMIT_TEST_MODE"))

logger = logging.getLogger("delimit.governance")


# ── STR-183 V2-hardening B-PREREQ-4: non-delegable operation registry ─
# Per /root/CLAUDE.md "Non-Delegable Decisions" and the 2026-04-07 ruleset-bypass postmortem,
# these operation classes can never be auto-approved by a generic gate (e.g. "all_gates_passed").
# Each invocation requires fresh, named-human attestation at gate-entry time.
# This constant is the code-level encoding of the constitutional boundary.
# Do not extend this set without an explicit founder-attested deliberation.
NON_DELEGABLE_OPERATION_CLASSES = frozenset({
    "ruleset_disable",       # disabling branch protection / repository rulesets
    "force_push_shared",     # force-push to main, release branches, or floating tags (v1, latest)
    "account_switch",        # switching gh / git author identity mid-flow
    "cross_account_ops",     # operating on one org from another org's identity
    "constitutional_rewrite",  # edits to founder doctrine canon outside managed sections
    "authority_class_expansion",  # adding a new class of tool / agent / gate
    "irreversible_capital_commit",  # capital commitments above non-delegable threshold
    "venture_kill",          # shutting down a Jamsons venture
    "permission_escalation",  # granting elevated access (sudo, admin, write-as-other)
    "public_truth_claim",    # public statement / marketing assertion outrunning evidence
})


def is_non_delegable(operation_class: str) -> bool:
    """Return True iff the operation class is in the non-delegable registry.

    Per the 2026-04-07 postmortem and the V2 pressure-test (STR-183, unanimous round 3),
    non-delegable operations cannot pass through any "all_gates_passed" mechanism.
    They require per-invocation founder attestation, checked live at gate entry.
    """
    return operation_class in NON_DELEGABLE_OPERATION_CLASSES


def require_founder_attestation(operation_class: str, attestation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fail-closed gate for non-delegable operations.

    Returns a verdict dict. The caller must refuse to proceed unless verdict["allowed"] is True.

    A valid attestation must include:
      - "founder_id": the named human performing the attestation
      - "scope": the exact operation being attested (must match operation_class)
      - "timestamp": ISO-8601 UTC
      - "evidence_ref": pointer to the evidence (ledger ID, postmortem path, or signed message)

    Pre-approval of a parent plan does NOT extend to non-delegable escalations
    (2026-04-07 postmortem rule). Each invocation needs its own attestation.
    """
    if not is_non_delegable(operation_class):
        return {"allowed": True, "operation_class": operation_class, "non_delegable": False}

    if not attestation:
        return {
            "allowed": False,
            "operation_class": operation_class,
            "non_delegable": True,
            "reason": (
                f"{operation_class} is non-delegable (STR-183 / 2026-04-07 postmortem). "
                "Pre-approval of a parent plan does not extend to this operation. "
                "Per-invocation founder attestation is required."
            ),
        }

    required = {"founder_id", "scope", "timestamp", "evidence_ref"}
    missing = required - set(attestation.keys())
    if missing:
        return {
            "allowed": False,
            "operation_class": operation_class,
            "non_delegable": True,
            "reason": f"Attestation missing required fields: {sorted(missing)}",
        }

    if attestation["scope"] != operation_class:
        return {
            "allowed": False,
            "operation_class": operation_class,
            "non_delegable": True,
            "reason": (
                f"Attestation scope mismatch: attested for '{attestation['scope']}' "
                f"but invocation is for '{operation_class}'. The scope of approval is "
                "the scope stated, not beyond (CLAUDE.md escalation rule)."
            ),
        }

    return {
        "allowed": True,
        "operation_class": operation_class,
        "non_delegable": True,
        "attestation": attestation,
    }


# ── LED-263: Beta CTA for conversion ────────────────────────────────
# Tools that should show a beta signup prompt on successful results.
_BETA_CTA_TOOLS = frozenset({"lint", "scan", "activate", "diff", "quickstart"})

_BETA_CTA = {
    "text": "Like what you see? Join the beta for priority support and full governance.",
    "url": "https://app.delimit.ai",
    "action": "star_repo_or_signup",
}


def _is_beta_user() -> bool:
    """Check if the current user is already tracked as a founding/beta user."""
    try:
        from ai.founding_users import _load_founding_users
        data = _load_founding_users()
        if data.get("users"):
            return True
    except Exception:
        pass
    # Also check if a Pro license is active (paying users don't need the CTA)
    try:
        from ai.license import get_license
        lic = get_license()
        if lic.get("tier", "free") != "free":
            return True
    except Exception:
        pass
    return False


def _result_is_successful(result: Dict[str, Any]) -> bool:
    """Return True if a tool result looks like a success (no errors)."""
    if result.get("error"):
        return False
    if result.get("status") in ("error", "failed", "blocked"):
        return False
    if result.get("governance_blocked"):
        return False
    return True


def _maybe_beta_cta(tool_name: str, result: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return a beta CTA dict if the tool qualifies and the user is not already signed up."""
    if tool_name not in _BETA_CTA_TOOLS:
        return None
    if not _result_is_successful(result):
        return None
    if _is_beta_user():
        return None
    return dict(_BETA_CTA)


def _ledger_list_items(project_path: str = ".") -> Dict[str, Any]:
    """Indirection layer so tests can patch governance-local ledger hooks."""
    import ai.ledger_manager as _lm
    return _lm.list_items(project_path=project_path)


def _ledger_add_item(*, title: str, type: str, priority: str, source: str, project_path: str = ".") -> Dict[str, Any]:
    """Indirection layer so tests can patch governance-local ledger hooks."""
    import ai.ledger_manager as _lm
    return _lm.add_item(
        title=title,
        type=type,
        priority=priority,
        source=source,
        project_path=project_path,
    )


def _ledger_update_item(item_id: str, *, status: str, project_path: str = ".") -> Dict[str, Any]:
    """Indirection layer so tests can patch governance-local ledger hooks."""
    import ai.ledger_manager as _lm
    return _lm.update_item(item_id, status=status, project_path=project_path)


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

# Milestone rules — auto-create DONE ledger items for significant completions.
# Unlike threshold RULES (which create open items for problems), milestones
# record achievements so the ledger reflects what was shipped.
MILESTONES = {
    "deploy_site": {
        "trigger_key": "status",
        "trigger_values": ["deployed"],
        "ledger_title": "Deployed: {project}",
        "ledger_type": "feat",
        "ledger_priority": "P1",
        "auto_done": True,
    },
    "deploy_npm": {
        "trigger_key": "status",
        "trigger_values": ["published"],
        "ledger_title": "Published: {package}@{new_version}",
        "ledger_type": "feat",
        "ledger_priority": "P1",
        "auto_done": True,
    },
    "deliberate": {
        "trigger_key": "status",
        "trigger_values": ["unanimous"],
        "ledger_title": "Consensus reached: {question_short}",
        "ledger_type": "strategy",
        "ledger_priority": "P1",
        "auto_done": True,
    },
    "test_generate": {
        "threshold_key": "tests_generated",
        "threshold": 10,
        "comparison": "above",
        "ledger_title": "Generated {value} tests",
        "ledger_type": "feat",
        "ledger_priority": "P2",
        "auto_done": True,
    },
    "sensor_github_issue": {
        "trigger_key": "has_new_activity",
        "trigger_if_true": True,
        "ledger_title": "Outreach activity: {repo}#{issue_number}",
        "ledger_type": "task",
        "ledger_priority": "P1",
        "auto_done": False,  # needs follow-up
    },
    "zero_spec": {
        "trigger_key": "success",
        "trigger_if_true": True,
        "ledger_title": "Zero-spec extracted: {framework} ({paths_count} paths)",
        "ledger_type": "feat",
        "ledger_priority": "P2",
        "auto_done": True,
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
        {"tool": "delimit_deploy_npm", "reason": "Publish the new version to npm", "premium": False},
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
    "deploy_npm": [
        {"tool": "delimit_deploy_verify", "reason": "Verify the published package", "premium": True},
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
    # Design & UI tools — triggered after UI-related work
    "deploy_site": [
        {"tool": "delimit_design_validate_responsive", "reason": "Check responsive design before deploy", "premium": False},
        {"tool": "delimit_story_accessibility", "reason": "Run accessibility audit", "premium": False},
        {"tool": "delimit_deploy_npm", "reason": "Publish npm package if applicable", "premium": False},
        {"tool": "delimit_ledger_context", "reason": "Check what else needs deploying", "premium": False},
    ],
    "design_component_library": [
        {"tool": "delimit_design_validate_responsive", "reason": "Validate responsive patterns", "premium": False},
        {"tool": "delimit_story_accessibility", "reason": "Check accessibility", "premium": False},
    ],
    "design_validate_responsive": [
        {"tool": "delimit_story_accessibility", "reason": "Also check accessibility", "premium": False},
        {"tool": "delimit_story_visual_test", "reason": "Take visual baseline screenshot", "premium": False},
    ],
    "design_generate_component": [
        {"tool": "delimit_story_generate", "reason": "Generate stories for the new component", "premium": False},
        {"tool": "delimit_design_validate_responsive", "reason": "Check responsive design", "premium": False},
    ],
    "story_accessibility": [
        {"tool": "delimit_ledger_add", "reason": "Track accessibility issues in ledger", "premium": False},
    ],
    "story_visual_test": [
        {"tool": "delimit_ledger_add", "reason": "Track visual regressions in ledger", "premium": False},
    ],
    "scan": [
        {"tool": "delimit_design_component_library", "reason": "Catalog UI components", "premium": False},
        {"tool": "delimit_design_validate_responsive", "reason": "Check responsive design", "premium": False},
        {"tool": "delimit_story_accessibility", "reason": "Run accessibility audit", "premium": False},
        {"tool": "delimit_gov_health", "reason": "Check governance status", "premium": True},
    ],
    "quickstart": [
        {"tool": "delimit_ledger_add", "reason": "Start tracking tasks in the ledger", "premium": False},
        {"tool": "delimit_lint", "reason": "Check an OpenAPI spec for breaking changes", "premium": False},
        {"tool": "delimit_deliberate", "reason": "Try multi-model deliberation on a decision", "premium": True},
        {"tool": "delimit_security_scan", "reason": "Run a security scan", "premium": True},
    ],
    "test_generate": [
        {"tool": "delimit_test_smoke", "reason": "Run smoke tests on generated tests", "premium": False},
        {"tool": "delimit_test_coverage", "reason": "Check coverage after adding tests", "premium": False},
    ],
    # --- Context & Memory workflow ---
    "context_init": [
        {"tool": "delimit_context_write", "reason": "Write your first artifact", "premium": True},
    ],
    "context_write": [
        {"tool": "delimit_context_list", "reason": "See all artifacts", "premium": True},
    ],
    "context_read": [],  # Terminal — user got what they needed
    "context_list": [],  # Terminal
    "context_snapshot": [
        {"tool": "delimit_ledger_context", "reason": "Check what else needs work", "premium": False},
    ],
    "context_branch": [],  # Terminal
    "memory_store": [
        {"tool": "delimit_ledger_context", "reason": "Check ledger after saving memory", "premium": False},
    ],
    "memory_search": [],  # Terminal — user got results
    "memory_recent": [],  # Terminal
    # --- Security workflow ---
    "security_scan": [
        {"tool": "delimit_security_audit", "reason": "Run full audit for details", "premium": False},
    ],
    "security_ingest": [
        {"tool": "delimit_security_deliberate", "reason": "Triage findings via multi-model deliberation", "premium": True},
        {"tool": "delimit_deploy_plan", "reason": "Check if deploys are gated by findings", "premium": True},
    ],
    "security_deliberate": [
        {"tool": "delimit_ledger_context", "reason": "Review updated security findings in ledger", "premium": False},
    ],
    # --- Governance deep workflow ---
    "gov_status": [
        {"tool": "delimit_gov_evaluate", "reason": "Evaluate compliance", "premium": True},
    ],
    "gov_evaluate": [
        {"tool": "delimit_gov_run", "reason": "Run governance checks", "premium": True},
    ],
    "gov_run": [
        {"tool": "delimit_gov_verify", "reason": "Verify results", "premium": True},
    ],
    "gov_verify": [
        {"tool": "delimit_ledger_context", "reason": "Check ledger for action items", "premium": False},
    ],
    "gov_policy": [],  # Terminal
    "gov_new_task": [
        {"tool": "delimit_ledger_context", "reason": "See updated ledger", "premium": False},
    ],
    # --- Deploy workflow (missing entries) ---
    "deploy_status": [
        {"tool": "delimit_deploy_verify", "reason": "Verify health", "premium": True},
    ],
    "deploy_rollback": [
        {"tool": "delimit_deploy_status", "reason": "Check rollback status", "premium": True},
    ],
    # --- Release workflow ---
    "release_plan": [
        {"tool": "delimit_release_validate", "reason": "Validate release readiness", "premium": True},
    ],
    "release_validate": [
        {"tool": "delimit_release_sync", "reason": "Sync across surfaces", "premium": True},
    ],
    "release_sync": [
        {"tool": "delimit_ledger_context", "reason": "Check for remaining items", "premium": False},
    ],
    "release_status": [],  # Terminal
    "release_history": [],  # Terminal
    "release_rollback": [
        {"tool": "delimit_deploy_status", "reason": "Verify rollback", "premium": True},
    ],
    # --- Observability workflow ---
    "obs_status": [
        {"tool": "delimit_obs_metrics", "reason": "See detailed metrics", "premium": True},
    ],
    "obs_metrics": [
        {"tool": "delimit_obs_logs", "reason": "Check logs for issues", "premium": True},
    ],
    "obs_logs": [
        {"tool": "delimit_obs_alerts", "reason": "Check active alerts", "premium": True},
    ],
    "obs_alerts": [],  # Terminal
    # --- Repo workflow ---
    "repo_diagnose": [
        {"tool": "delimit_repo_analyze", "reason": "Full analysis", "premium": True},
    ],
    "repo_config_validate": [
        {"tool": "delimit_repo_config_audit", "reason": "Audit for security issues", "premium": True},
    ],
    "repo_config_audit": [
        {"tool": "delimit_security_audit", "reason": "Full security scan", "premium": False},
    ],
    # --- Docs workflow ---
    "docs_generate": [
        {"tool": "delimit_docs_validate", "reason": "Validate generated docs", "premium": False},
    ],
    "docs_validate": [
        {"tool": "delimit_ledger_context", "reason": "Check for doc-related tasks", "premium": False},
    ],
    # --- Cost workflow ---
    "cost_analyze": [
        {"tool": "delimit_cost_optimize", "reason": "Find optimization opportunities", "premium": True},
    ],
    "cost_optimize": [
        {"tool": "delimit_cost_alert", "reason": "Set cost alerts", "premium": True},
    ],
    "cost_alert": [],  # Terminal
    # --- Data workflow ---
    "data_validate": [
        {"tool": "delimit_data_backup", "reason": "Backup validated data", "premium": True},
    ],
    "data_backup": [],  # Terminal
    "data_migrate": [
        {"tool": "delimit_data_validate", "reason": "Validate after migration", "premium": True},
    ],
    # --- Secrets workflow ---
    "secret_store": [
        {"tool": "delimit_secret_list", "reason": "Verify stored secrets", "premium": True},
    ],
    "secret_get": [],  # Terminal
    "secret_list": [],  # Terminal
    "secret_revoke": [],  # Terminal
    "secret_access_log": [],  # Terminal
    # --- Intel workflow ---
    "intel_query": [],  # Terminal
    "intel_dataset_register": [
        {"tool": "delimit_intel_dataset_list", "reason": "Verify registration", "premium": True},
    ],
    "intel_dataset_list": [],  # Terminal
    "intel_dataset_freeze": [],  # Terminal
    "intel_snapshot_ingest": [
        {"tool": "delimit_intel_query", "reason": "Query the ingested data", "premium": True},
    ],
    # --- Social/Content workflow ---
    "social_post": [
        {"tool": "delimit_social_history", "reason": "Check post history", "premium": True},
    ],
    "social_generate": [
        {"tool": "delimit_social_post", "reason": "Post the generated content", "premium": True},
    ],
    "social_history": [],  # Terminal
    "content_publish": [
        {"tool": "delimit_content_schedule", "reason": "Check upcoming schedule", "premium": True},
    ],
    "content_schedule": [],  # Terminal
    "content_queue": [],  # Terminal
    # --- OS/Daemon workflow ---
    "os_status": [
        {"tool": "delimit_os_plan", "reason": "Plan next OS actions", "premium": True},
    ],
    "os_plan": [
        {"tool": "delimit_os_gates", "reason": "Check gates", "premium": True},
    ],
    "os_gates": [],  # Terminal
    "daemon_status": [],  # Terminal
    "daemon_run": [
        {"tool": "delimit_daemon_status", "reason": "Check results", "premium": True},
    ],
    "daemon_classify": [],  # Terminal
    # --- Resource/Vault/Misc ---
    "resource_list": [],  # Terminal
    "resource_get": [],  # Terminal
    "resource_drivers": [],  # Terminal
    "vault_health": [],  # Terminal
    "vault_search": [],  # Terminal
    "vault_snapshot": [],  # Terminal
    "sensor_github_issue": [
        {"tool": "delimit_ledger_context", "reason": "Check ledger for outreach items", "premium": False},
    ],
    "evidence_collect": [
        {"tool": "delimit_evidence_verify", "reason": "Verify collected evidence", "premium": True},
    ],
    "evidence_verify": [],  # Terminal
    "generate_scaffold": [
        {"tool": "delimit_init", "reason": "Initialize governance for the new project", "premium": False},
    ],
    "generate_template": [],  # Terminal
    # --- Terminal tools ---
    "help": [],
    "version": [],
    "license_status": [],
    "activate": [],
    "ventures": [],
    "ledger": [],
    "ledger_list": [],
    "ledger_done": [
        {"tool": "delimit_ledger_context", "reason": "See what's next", "premium": False},
    ],
    "ledger_context": [],  # Entry point — don't chain from it
    "policy": [],
    "explain": [],
    "impact": [],
    "zero_spec": [
        {"tool": "delimit_lint", "reason": "Lint the extracted spec", "premium": False},
    ],
    "models": [],
    "story_build": [],
    "story_generate": [
        {"tool": "delimit_story_accessibility", "reason": "Check accessibility", "premium": False},
    ],
    # --- Design extras (not yet routed) ---
    "design_extract_tokens": [
        {"tool": "delimit_design_generate_tailwind", "reason": "Generate Tailwind config from tokens", "premium": False},
    ],
    "design_generate_tailwind": [
        {"tool": "delimit_design_validate_responsive", "reason": "Validate responsive design", "premium": False},
    ],
    # --- Test extras ---
    "test_smoke": [
        {"tool": "delimit_test_coverage", "reason": "Check coverage after smoke tests", "premium": False},
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

    # 1b. Check milestone rules (auto-create DONE items for achievements)
    milestone = MILESTONES.get(clean_name)
    if milestone:
        m_triggered = False
        m_context = {}

        # Value match (e.g., status == "deployed")
        if "trigger_key" in milestone and "trigger_values" in milestone:
            value = _deep_get(result, milestone["trigger_key"])
            if value in milestone["trigger_values"]:
                m_triggered = True
                m_context = {"value": str(value)}

        # Boolean check (e.g., success == True)
        if "trigger_key" in milestone and milestone.get("trigger_if_true"):
            value = _deep_get(result, milestone["trigger_key"])
            if value:
                m_triggered = True

        # Threshold above (e.g., tests_generated > 10)
        if "threshold_key" in milestone:
            value = _deep_get(result, milestone["threshold_key"])
            if value is not None:
                threshold = milestone["threshold"]
                if milestone.get("comparison") == "above" and value > threshold:
                    m_triggered = True
                    m_context = {"value": str(value), "threshold": str(threshold)}

        if m_triggered:
            # Build context from result fields for title interpolation
            for key in ("project", "package", "new_version", "framework", "paths_count", "repo", "issue_number"):
                if key not in m_context:
                    v = _deep_get(result, key)
                    if v is not None:
                        m_context[key] = str(v)
            # Special: short question for deliberations
            if "question_short" not in m_context:
                q = _deep_get(result, "question") or _deep_get(result, "note") or ""
                m_context["question_short"] = str(q)[:80]

            try:
                title = milestone["ledger_title"].format(**m_context)
            except (KeyError, IndexError):
                title = milestone["ledger_title"]

            auto_items.append({
                "title": title,
                "type": milestone.get("ledger_type", "feat"),
                "priority": milestone.get("ledger_priority", "P1"),
                "source": f"milestone:{clean_name}",
                "auto_done": milestone.get("auto_done", True),
            })

    # 2. Auto-create ledger items (with dedup — skip if open item with same title exists)
    if auto_items:
        try:
            # Load existing open titles for dedup
            existing = _ledger_list_items(project_path=project_path)
            # items can be a list or dict of lists (by ledger type)
            all_items = []
            raw_items = existing.get("items", [])
            if isinstance(raw_items, dict):
                for ledger_items in raw_items.values():
                    if isinstance(ledger_items, list):
                        all_items.extend(ledger_items)
            elif isinstance(raw_items, list):
                all_items = raw_items
            open_titles = {
                i.get("title", "")
                for i in all_items
                if isinstance(i, dict) and i.get("status") == "open"
            }
            created = []
            test_mode = _is_test_mode()
            for item in auto_items:
                if item["title"] in open_titles:
                    logger.debug("Skipping duplicate ledger item: %s", item["title"])
                    continue
                if test_mode:
                    # In test mode, skip real ledger writes to avoid
                    # polluting the project ledger with mock/test data.
                    logger.debug("Test mode: skipping ledger write for %s", item["title"])
                    created.append(f"TEST-{item['title'][:40]}")
                    continue
                entry = _ledger_add_item(
                    title=item["title"],
                    type=item["type"],
                    priority=item["priority"],
                    source=item["source"],
                    project_path=project_path,
                )
                item_id = entry.get("added", {}).get("id", "")
                created.append(item_id)
                # Auto-close milestone items
                if item.get("auto_done") and item_id:
                    try:
                        _ = _ledger_update_item(item_id, status="done", project_path=project_path)
                    except Exception:
                        pass
            governed_result["governance"] = {
                "action": "ledger_items_created",
                "items": created,
                "reason": "Governance rule triggered by tool result",
            }
        except Exception as e:
            logger.warning("Governance auto-ledger failed: %s", e)

    # 3. Add governance-directed next steps
    steps = NEXT_STEPS.get(clean_name, [])
    if steps:
        governed_result["next_steps"] = steps

    # 4. GOVERNANCE LOOP: always route back to ledger_context
    # This is not a suggestion — it's how the loop works.
    # The AI should call ledger_context after every tool to check what's next.
    # Ledger tools now route through governance for next_steps but skip auto-create
    # (no rules/milestones defined for them, so no recursion risk)
    SKIP_GOVERNANCE_LOOP = ("ventures", "version", "help", "diagnose", "activate", "license_status", "models", "scan")
    if clean_name not in SKIP_GOVERNANCE_LOOP:
        if "next_steps" not in governed_result:
            governed_result["next_steps"] = []
        # Don't suggest ledger_context to itself (circular)
        if clean_name != "ledger_context":
            existing = {s.get("tool") for s in governed_result.get("next_steps", [])}
            if "delimit_ledger_context" not in existing:
                governed_result["next_steps"].insert(0, {
                    "tool": "delimit_ledger_context",
                    "reason": "GOVERNANCE LOOP: check ledger for next action",
                    "premium": False,
                    "required": True,
                })
    else:
        # Excluded tools still get the next_steps field (empty) for schema consistency
        if "next_steps" not in governed_result:
            governed_result["next_steps"] = []

    # LED-263: Beta CTA on successful lint/scan/activate/diff results
    cta = _maybe_beta_cta(clean_name, governed_result)
    if cta:
        governed_result["beta_cta"] = cta

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
