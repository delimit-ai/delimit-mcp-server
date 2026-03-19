#!/usr/bin/env python3
"""
Delimit Unified MCP Server v2.0
Single agent-facing surface for the entire Delimit platform.

Architecture:
  Agent ──→ [this server] ──→ backends/ ──→ gateway core / OS / memory / vault / ...

Tier 1 (Core):       delimit.lint, delimit.diff, delimit.policy, delimit.ledger, delimit.impact
Tier 2 (Platform):   delimit.os.*, delimit.memory.*, delimit.vault.*, delimit.gov.*
Tier 3 (Extended):   delimit.deploy.*, delimit.intel.*, delimit.generate.*, delimit.repo.*,
                     delimit.security.*, delimit.evidence.*
Tier 4 (Ops/UI):    delimit.release.*, delimit.cost.*, delimit.data.*, delimit.obs.*,
                     delimit.design.*, delimit.story.*, delimit.test.*, delimit.docs.*

All tools follow the Adapter Boundary Contract v1.0:
- Pure translation (zero governance logic in this file)
- Deterministic errors on failure
- Stateless between calls
"""

import json
import logging
import os
import shutil
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

logger = logging.getLogger("delimit.ai")

mcp = FastMCP("delimit")
mcp.description = "Delimit — The smart lint engine for OpenAPI. Unified agent surface."

VERSION = "3.2.0"

# LED-044: Hide STUB and PASS-THROUGH tools from MCP unless opted in.
# Set DELIMIT_SHOW_EXPERIMENTAL=1 to expose all tools (internal development).
SHOW_EXPERIMENTAL = os.environ.get("DELIMIT_SHOW_EXPERIMENTAL", "") == "1"


def _experimental_tool():
    """Decorator that only registers the function as an MCP tool if SHOW_EXPERIMENTAL is set.
    When disabled, the function still exists but is not exposed via MCP."""
    def decorator(fn):
        if SHOW_EXPERIMENTAL:
            return mcp.tool()(fn)
        return fn
    return decorator


def _safe_call(fn, **kwargs) -> Dict[str, Any]:
    """Wrap backend calls with deterministic error handling."""
    try:
        return fn(**kwargs)
    except FileNotFoundError as e:
        return {"error": "file_not_found", "message": str(e)}
    except Exception as e:
        logger.error("Backend error: %s\n%s", e, traceback.format_exc())
        return {"error": "backend_failure", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  CONSENSUS 096: Tool Cohesion — next_steps in every response
# ═══════════════════════════════════════════════════════════════════════

NEXT_STEPS_REGISTRY: Dict[str, List[Dict[str, Any]]] = {
    # --- Tier 1 Core (Free) ---
    "lint": [
        {"tool": "delimit_explain", "reason": "Get migration guide for breaking changes", "suggested_args": {"template": "migration"}, "is_premium": False},
        {"tool": "delimit_semver", "reason": "Determine the version bump for these changes", "suggested_args": {}, "is_premium": False},
    ],
    "diff": [
        {"tool": "delimit_semver", "reason": "Classify the semver bump for these changes", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_policy", "reason": "Check policy violations for these changes", "suggested_args": {}, "is_premium": False},
    ],
    "policy": [
        {"tool": "delimit_lint", "reason": "Run full lint with policy enforcement", "suggested_args": {}, "is_premium": False},
    ],
    "ledger": [],
    "impact": [
        {"tool": "delimit_ledger", "reason": "Record this impact assessment in the audit ledger", "suggested_args": {}, "is_premium": False},
    ],
    "semver": [
        {"tool": "delimit_explain", "reason": "Generate human-readable changelog for the version bump", "suggested_args": {"template": "changelog"}, "is_premium": False},
    ],
    "explain": [],
    "zero_spec": [
        {"tool": "delimit_lint", "reason": "Lint the extracted spec against your baseline", "suggested_args": {}, "is_premium": False},
    ],
    "init": [
        {"tool": "delimit_gov_health", "reason": "Verify governance health after initialization", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_diagnose", "reason": "Check environment and tool status", "suggested_args": {}, "is_premium": False},
    ],
    # --- Tier 2 Platform (Pro) ---
    "os_plan": [],
    "os_status": [],
    "os_gates": [],
    "gov_health": [
        {"tool": "delimit_gov_status", "reason": "Get detailed governance status", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_repo_analyze", "reason": "Analyze repository structure and quality", "suggested_args": {}, "is_premium": True},
    ],
    "gov_status": [
        {"tool": "delimit_gov_policy", "reason": "Review governance policy configuration", "suggested_args": {}, "is_premium": True},
    ],
    "gov_policy": [],
    "gov_evaluate": [],
    "gov_new_task": [],
    "gov_run": [],
    "gov_verify": [],
    "memory_search": [
        {"tool": "delimit_memory_store", "reason": "Store new information if no results found", "suggested_args": {}, "is_premium": True},
    ],
    "memory_store": [],
    "memory_recent": [],
    "vault_search": [],
    "vault_health": [
        {"tool": "delimit_vault_search", "reason": "Search vault entries for details", "suggested_args": {}, "is_premium": True},
    ],
    "vault_snapshot": [],
    # --- Tier 3 Extended (Pro) ---
    "deploy_plan": [
        {"tool": "delimit_deploy_build", "reason": "Build Docker images for deployment", "suggested_args": {}, "is_premium": True},
    ],
    "deploy_build": [
        {"tool": "delimit_deploy_publish", "reason": "Publish built images to registry", "suggested_args": {}, "is_premium": True},
    ],
    "deploy_publish": [
        {"tool": "delimit_deploy_verify", "reason": "Verify deployment health after publish", "suggested_args": {}, "is_premium": True},
    ],
    "deploy_rollback": [],
    "deploy_status": [],
    "generate_template": [],
    "generate_scaffold": [],
    "security_scan": [
        {"tool": "delimit_evidence_collect", "reason": "Collect evidence artifacts from security findings", "suggested_args": {}, "is_premium": True},
    ],
    "evidence_collect": [
        {"tool": "delimit_evidence_verify", "reason": "Verify evidence bundle integrity", "suggested_args": {}, "is_premium": True},
    ],
    "evidence_verify": [],
    "security_audit": [
        {"tool": "delimit_security_scan", "reason": "Run deeper security scan on flagged areas", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_evidence_collect", "reason": "Collect evidence of security findings", "suggested_args": {}, "is_premium": True},
    ],
    # --- Tier 4 Ops ---
    "obs_status": [
        {"tool": "delimit_obs_metrics", "reason": "Get detailed CPU/memory/disk metrics", "suggested_args": {"query": "all"}, "is_premium": False},
        {"tool": "delimit_obs_logs", "reason": "Search logs for errors or issues", "suggested_args": {"query": "error"}, "is_premium": False},
    ],
    "obs_metrics": [
        {"tool": "delimit_obs_logs", "reason": "Correlate metrics with log entries", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_obs_status", "reason": "Get overall system health", "suggested_args": {}, "is_premium": False},
    ],
    "obs_logs": [
        {"tool": "delimit_obs_metrics", "reason": "Check system metrics for the same time range", "suggested_args": {}, "is_premium": False},
    ],
    "release_plan": [
        {"tool": "delimit_release_status", "reason": "Check current deploy status before releasing", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_security_audit", "reason": "Audit security before release", "suggested_args": {}, "is_premium": False},
    ],
    "release_status": [
        {"tool": "delimit_release_plan", "reason": "Create a new release plan", "suggested_args": {}, "is_premium": False},
    ],
    "cost_analyze": [
        {"tool": "delimit_cost_optimize", "reason": "Find optimization opportunities for detected services", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_cost_alert", "reason": "Set up cost threshold alerts", "suggested_args": {"action": "create"}, "is_premium": False},
    ],
    "cost_optimize": [
        {"tool": "delimit_cost_analyze", "reason": "Get full cost breakdown for the project", "suggested_args": {}, "is_premium": False},
    ],
    "cost_alert": [],
    "data_validate": [
        {"tool": "delimit_data_backup", "reason": "Back up validated data files", "suggested_args": {}, "is_premium": False},
    ],
    "data_migrate": [
        {"tool": "delimit_data_validate", "reason": "Validate data integrity after migration check", "suggested_args": {}, "is_premium": False},
    ],
    "data_backup": [],
    "intel_dataset_register": [
        {"tool": "delimit_intel_snapshot_ingest", "reason": "Ingest data into the registered dataset", "suggested_args": {}, "is_premium": False},
    ],
    "intel_dataset_list": [],
    "intel_dataset_freeze": [],
    "intel_snapshot_ingest": [
        {"tool": "delimit_intel_query", "reason": "Query ingested snapshots", "suggested_args": {}, "is_premium": False},
    ],
    "intel_query": [],
    "test_generate": [
        {"tool": "delimit_test_smoke", "reason": "Run the generated tests to verify they pass", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_docs_generate", "reason": "Generate API docs for the tested code", "suggested_args": {}, "is_premium": False},
    ],
    "test_smoke": [
        {"tool": "delimit_test_generate", "reason": "Generate test skeletons for untested files", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_docs_validate", "reason": "Check documentation coverage alongside test coverage", "suggested_args": {}, "is_premium": False},
    ],
    "docs_generate": [
        {"tool": "delimit_docs_validate", "reason": "Validate the generated documentation for completeness", "suggested_args": {}, "is_premium": False},
    ],
    "docs_validate": [
        {"tool": "delimit_docs_generate", "reason": "Generate docs to fix missing documentation", "suggested_args": {}, "is_premium": False},
        {"tool": "delimit_test_generate", "reason": "Generate tests alongside documentation improvements", "suggested_args": {}, "is_premium": False},
    ],
    # --- Tier 4 Design/Story ---
    "design_extract_tokens": [
        {"tool": "delimit_design_generate_tailwind", "reason": "Generate Tailwind config from extracted tokens", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_design_component_library", "reason": "Catalog components that use these tokens", "suggested_args": {}, "is_premium": True},
    ],
    "design_generate_component": [
        {"tool": "delimit_story_generate", "reason": "Generate stories for the new component", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_story_accessibility", "reason": "Check accessibility of the generated component", "suggested_args": {}, "is_premium": True},
    ],
    "design_generate_tailwind": [
        {"tool": "delimit_design_extract_tokens", "reason": "Extract tokens to verify config coverage", "suggested_args": {}, "is_premium": True},
    ],
    "design_validate_responsive": [
        {"tool": "delimit_story_visual_test", "reason": "Take screenshots at different viewports", "suggested_args": {}, "is_premium": True},
    ],
    "design_component_library": [
        {"tool": "delimit_story_generate", "reason": "Generate stories for cataloged components", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_story_accessibility", "reason": "Run accessibility audit on all components", "suggested_args": {}, "is_premium": True},
    ],
    "story_generate": [
        {"tool": "delimit_story_visual_test", "reason": "Capture visual baseline for the component", "suggested_args": {}, "is_premium": True},
        {"tool": "delimit_story_accessibility", "reason": "Check accessibility of the component", "suggested_args": {}, "is_premium": True},
    ],
    "story_visual_test": [
        {"tool": "delimit_story_accessibility", "reason": "Also run accessibility checks", "suggested_args": {}, "is_premium": True},
    ],
    "story_accessibility": [
        {"tool": "delimit_design_validate_responsive", "reason": "Also validate responsive patterns", "suggested_args": {}, "is_premium": True},
    ],
    # --- Sensing ---
    "sensor_github_issue": [],
    # --- Meta ---
    "version": [],
    "help": [],
    "diagnose": [],
    "activate": [],
    "license_status": [],
}


def _with_next_steps(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Route every tool result through governance (replaces simple next_steps).

    Governance:
    1. Checks result against rules (thresholds, policies)
    2. Auto-creates ledger items for failures/warnings
    3. Adds next_steps to keep the AI building
    4. Loops back to governance via ledger_context suggestion
    """
    try:
        from ai.governance import govern
        return govern(tool_name, result)
    except Exception:
        # Fallback: just add next_steps from registry
        steps = NEXT_STEPS_REGISTRY.get(tool_name, [])
        result["next_steps"] = steps
        return result


# ═══════════════════════════════════════════════════════════════════════
#  TIER 1: CORE — API Lint Engine
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
def delimit_lint(old_spec: str, new_spec: str, policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Lint two OpenAPI specs for breaking changes and policy violations.
    Primary CI integration point. Combines diff + policy into pass/fail.

    Args:
        old_spec: Path to the old (baseline) OpenAPI spec file.
        new_spec: Path to the new (proposed) OpenAPI spec file.
        policy_file: Optional path to a .delimit/policies.yml file.
    """
    from backends.gateway_core import run_lint
    return _with_next_steps("lint", _safe_call(run_lint, old_spec=old_spec, new_spec=new_spec, policy_file=policy_file))


@mcp.tool()
def delimit_diff(old_spec: str, new_spec: str) -> Dict[str, Any]:
    """Diff two OpenAPI specs and list all changes. Pure diff, no policy.

    Args:
        old_spec: Path to the old OpenAPI spec file.
        new_spec: Path to the new OpenAPI spec file.
    """
    from backends.gateway_core import run_diff
    return _with_next_steps("diff", _safe_call(run_diff, old_spec=old_spec, new_spec=new_spec))


@mcp.tool()
def delimit_policy(spec_files: List[str], policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Inspect or validate governance policy configuration.

    Args:
        spec_files: List of spec file paths.
        policy_file: Optional custom policy file path.
    """
    from backends.gateway_core import run_policy
    return _with_next_steps("policy", _safe_call(run_policy, spec_files=spec_files, policy_file=policy_file))


@mcp.tool()
def delimit_ledger(ledger_path: str, api_name: Optional[str] = None, repository: Optional[str] = None, validate_chain: bool = False) -> Dict[str, Any]:
    """Query the append-only contract ledger (hash-chained JSONL).

    Args:
        ledger_path: Path to the events.jsonl ledger file.
        api_name: Filter events by API name.
        repository: Filter events by repository.
        validate_chain: Validate hash chain integrity.
    """
    from backends.gateway_core import query_ledger
    return _with_next_steps("ledger", _safe_call(query_ledger, ledger_path=ledger_path, api_name=api_name, repository=repository, validate_chain=validate_chain))


@mcp.tool()
def delimit_impact(api_name: str, dependency_file: Optional[str] = None) -> Dict[str, Any]:
    """Analyze downstream impact of an API change. Informational only.

    Args:
        api_name: The API that changed.
        dependency_file: Optional path to dependency manifest.
    """
    from backends.gateway_core import run_impact
    return _with_next_steps("impact", _safe_call(run_impact, api_name=api_name, dependency_file=dependency_file))


@mcp.tool()
def delimit_semver(old_spec: str, new_spec: str, current_version: Optional[str] = None) -> Dict[str, Any]:
    """Classify the semver bump for a spec change (MAJOR/MINOR/PATCH/NONE).

    Deterministic classification based on diff engine output.
    Optionally computes the next version string.

    Args:
        old_spec: Path to the old OpenAPI spec file.
        new_spec: Path to the new OpenAPI spec file.
        current_version: Optional current version (e.g. "1.2.3") to compute next version.
    """
    from backends.gateway_core import run_semver
    return _with_next_steps("semver", _safe_call(run_semver, old_spec=old_spec, new_spec=new_spec, current_version=current_version))


@mcp.tool()
def delimit_explain(
    old_spec: str,
    new_spec: str,
    template: str = "developer",
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a human-readable explanation of API changes.

    7 templates: developer, team_lead, product, migration, changelog, pr_comment, slack.

    Args:
        old_spec: Path to the old OpenAPI spec file.
        new_spec: Path to the new OpenAPI spec file.
        template: Template name (default: developer).
        old_version: Previous version string.
        new_version: New version string.
        api_name: API/service name for context.
    """
    from backends.gateway_core import run_explain
    return _with_next_steps("explain", _safe_call(run_explain, old_spec=old_spec, new_spec=new_spec, template=template, old_version=old_version, new_version=new_version, api_name=api_name))


@mcp.tool()
def delimit_zero_spec(
    project_dir: str = ".",
    python_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract OpenAPI spec from framework source code (no spec file needed).

    Detects the API framework (FastAPI, Express, NestJS) and extracts a
    complete OpenAPI specification directly from the source code.
    Currently supports FastAPI with full fidelity.

    Args:
        project_dir: Path to the project root directory.
        python_bin: Optional Python binary path (auto-detected if omitted).
    """
    from backends.gateway_core import run_zero_spec
    return _with_next_steps("zero_spec", _safe_call(run_zero_spec, project_dir=project_dir, python_bin=python_bin))




@mcp.tool()
def delimit_init(
    project_path: str = ".",
    preset: str = "default",
) -> Dict[str, Any]:
    """Initialize Delimit governance for a project. Creates .delimit/policies.yml and ledger directory.

    Args:
        project_path: Project root directory.
        preset: Policy preset — strict, default, or relaxed.
    """
    VALID_PRESETS = ("strict", "default", "relaxed")
    if preset not in VALID_PRESETS:
        return {
            "error": "invalid_preset",
            "message": f"Preset must be one of {VALID_PRESETS}, got '{preset}'",
        }

    root = Path(project_path).resolve()
    delimit_dir = root / ".delimit"
    policies_file = delimit_dir / "policies.yml"
    ledger_dir = delimit_dir / "ledger"
    events_file = ledger_dir / "events.jsonl"

    # Idempotency check
    if policies_file.exists() and ledger_dir.exists() and events_file.exists():
        return _with_next_steps("init", {
            "tool": "init",
            "status": "already_initialized",
            "project_path": str(root),
            "preset": preset,
            "message": f"Project already initialized at {delimit_dir}. No files overwritten.",
        })

    created = []

    # 1. Create .delimit/ directory
    if not delimit_dir.exists():
        delimit_dir.mkdir(parents=True, exist_ok=True)
        created.append(str(delimit_dir))

    # 2. Create policies.yml from preset
    if not policies_file.exists():
        preset_source = Path(__file__).resolve().parent.parent / "core" / "policies" / f"{preset}.yml"
        if preset_source.exists():
            shutil.copy2(str(preset_source), str(policies_file))
        else:
            # Fallback: write a minimal default
            fallback_content = (
                    f"# Delimit Policy Preset: {preset}\n"
                    "# Generated by delimit_init\n"
                    "override_defaults: false\n"
                    "rules: []\n"
                )
            policies_file.write_text(fallback_content)
        created.append(str(policies_file))

    # 3. Create ledger directory
    if not ledger_dir.exists():
        ledger_dir.mkdir(parents=True, exist_ok=True)
        created.append(str(ledger_dir))

    # 4. Create empty events.jsonl
    if not events_file.exists():
        events_file.touch()
        created.append(str(events_file))

    return _with_next_steps("init", {
        "tool": "init",
        "status": "initialized",
        "project_path": str(root),
        "preset": preset,
        "created": created,
        "message": f"Governance initialized with '{preset}' preset. {len(created)} items created.",
    })

# ═══════════════════════════════════════════════════════════════════════
#  TIER 2: PLATFORM — OS, Governance, Memory, Vault
# ═══════════════════════════════════════════════════════════════════════


# ─── OS ─────────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_os_plan(operation: str, target: str, parameters: Optional[Dict[str, Any]] = None, require_approval: bool = True) -> Dict[str, Any]:
    """Create a governed execution plan (Pro).

    Args:
        operation: Operation to plan (e.g. "deploy", "migrate").
        target: Target component or service.
        parameters: Operation parameters.
        require_approval: Whether to require approval before execution.
    """
    from ai.license import require_premium
    gate = require_premium("os_plan")
    if gate:
        return gate
    from backends.os_bridge import create_plan
    return _with_next_steps("os_plan", _safe_call(create_plan, operation=operation, target=target, parameters=parameters, require_approval=require_approval))


@mcp.tool()
def delimit_os_status() -> Dict[str, Any]:
    """Get current Delimit OS status with plan/task/token counts (Pro)."""
    from ai.license import require_premium
    gate = require_premium("os_status")
    if gate:
        return gate
    from backends.os_bridge import get_status
    return _with_next_steps("os_status", _safe_call(get_status))


@mcp.tool()
def delimit_os_gates(plan_id: str) -> Dict[str, Any]:
    """Check governance gates for a plan (Pro).

    Args:
        plan_id: The plan ID (e.g. "PLAN-A1B2C3D4").
    """
    from ai.license import require_premium
    gate = require_premium("os_gates")
    if gate:
        return gate
    from backends.os_bridge import check_gates
    return _with_next_steps("os_gates", _safe_call(check_gates, plan_id=plan_id))


# ─── Governance ─────────────────────────────────────────────────────────

@mcp.tool()
def delimit_gov_health(repo: str = ".") -> Dict[str, Any]:
    """Check governance system health (Pro).

    Args:
        repo: Repository path to check.
    """
    from ai.license import require_premium
    gate = require_premium("gov_health")
    if gate:
        return gate
    from backends.governance_bridge import health
    return _with_next_steps("gov_health", _safe_call(health, repo=repo))


@mcp.tool()
def delimit_gov_status(repo: str = ".") -> Dict[str, Any]:
    """Get current governance status for a repository (Pro).

    Args:
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_status")
    if gate:
        return gate
    from backends.governance_bridge import status
    return _with_next_steps("gov_status", _safe_call(status, repo=repo))


@mcp.tool()
def delimit_gov_policy(repo: str = ".") -> Dict[str, Any]:
    """Get governance policy for a repository (Pro).

    Args:
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_policy")
    if gate:
        return gate
    from backends.governance_bridge import policy
    return _with_next_steps("gov_policy", _safe_call(policy, repo=repo))


@mcp.tool()
def delimit_gov_evaluate(action: str, context: Optional[Dict[str, Any]] = None, repo: str = ".") -> Dict[str, Any]:
    """Evaluate if governance is required for an action (requires governancegate) (Pro).

    Args:
        action: The action to evaluate.
        context: Additional context.
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_evaluate")
    if gate:
        return gate
    from backends.governance_bridge import evaluate_trigger
    return _with_next_steps("gov_evaluate", _safe_call(evaluate_trigger, action=action, context=context, repo=repo))


@mcp.tool()
def delimit_gov_new_task(title: str, scope: str, risk_level: str = "medium", repo: str = ".") -> Dict[str, Any]:
    """Create a new governance task (requires governancegate) (Pro).

    Args:
        title: Task title.
        scope: Task scope.
        risk_level: Risk level (low/medium/high/critical).
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_new_task")
    if gate:
        return gate
    from backends.governance_bridge import new_task
    return _with_next_steps("gov_new_task", _safe_call(new_task, title=title, scope=scope, risk_level=risk_level, repo=repo))


@mcp.tool()
def delimit_gov_run(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Run a governance task (requires governancegate) (Pro).

    Args:
        task_id: Task ID to run.
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_run")
    if gate:
        return gate
    from backends.governance_bridge import run_task
    return _with_next_steps("gov_run", _safe_call(run_task, task_id=task_id, repo=repo))


@mcp.tool()
def delimit_gov_verify(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Verify a governance task (requires governancegate) (Pro).

    Args:
        task_id: Task ID to verify.
        repo: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("gov_verify")
    if gate:
        return gate
    from backends.governance_bridge import verify
    return _with_next_steps("gov_verify", _safe_call(verify, task_id=task_id, repo=repo))


# ─── Memory ─────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_memory_search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search conversation memory semantically (Pro).

    Args:
        query: Natural language search query.
        limit: Maximum results to return.
    """
    from ai.license import require_premium
    gate = require_premium("memory_search")
    if gate:
        return gate
    from backends.memory_bridge import search
    return _with_next_steps("memory_search", _safe_call(search, query=query, limit=limit))


@mcp.tool()
def delimit_memory_store(content: str, tags: Optional[List[str]] = None, context: Optional[str] = None) -> Dict[str, Any]:
    """Store a memory entry for future retrieval (Pro).

    Args:
        content: The content to remember.
        tags: Optional categorization tags.
        context: Optional context about when/why this was stored.
    """
    from ai.license import require_premium
    gate = require_premium("memory_store")
    if gate:
        return gate
    from backends.memory_bridge import store
    return _with_next_steps("memory_store", _safe_call(store, content=content, tags=tags, context=context))


@mcp.tool()
def delimit_memory_recent(limit: int = 5) -> Dict[str, Any]:
    """Get recent work summary from memory (Pro).

    Args:
        limit: Number of recent entries to return.
    """
    from ai.license import require_premium
    gate = require_premium("memory_recent")
    if gate:
        return gate
    from backends.memory_bridge import get_recent
    return _with_next_steps("memory_recent", _safe_call(get_recent, limit=limit))


# ─── Vault ──────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_vault_search(query: str) -> Dict[str, Any]:
    """Search vault entries (Pro).

    Args:
        query: Search query for vault entries.
    """
    from ai.license import require_premium
    gate = require_premium("vault_search")
    if gate:
        return gate
    from backends.vault_bridge import search
    return _with_next_steps("vault_search", _safe_call(search, query=query))


@mcp.tool()
def delimit_vault_health() -> Dict[str, Any]:
    """Check vault health status (Pro)."""
    from ai.license import require_premium
    gate = require_premium("vault_health")
    if gate:
        return gate
    from backends.vault_bridge import health
    return _with_next_steps("vault_health", _safe_call(health))


@mcp.tool()
def delimit_vault_snapshot() -> Dict[str, Any]:
    """Get a vault state snapshot (Pro)."""
    from ai.license import require_premium
    gate = require_premium("vault_snapshot")
    if gate:
        return gate
    from backends.vault_bridge import snapshot
    return _with_next_steps("vault_snapshot", _safe_call(snapshot))


# ═══════════════════════════════════════════════════════════════════════
#  TIER 3: EXTENDED — Deploy, Intel, Generate, Repo, Security, Evidence
# ═══════════════════════════════════════════════════════════════════════


# ─── Deploy ─────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_deploy_plan(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Plan deployment with build steps (Pro).

    Args:
        app: Application name.
        env: Target environment (staging/production).
        git_ref: Git reference (branch, tag, or SHA).
    """
    from ai.license import require_premium
    gate = require_premium("deploy_plan")
    if gate:
        return gate
    from backends.deploy_bridge import plan
    return _with_next_steps("deploy_plan", _safe_call(plan, app=app, env=env, git_ref=git_ref))


@mcp.tool()
def delimit_deploy_build(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Build Docker images with SHA tags (Pro).

    Args:
        app: Application name.
        git_ref: Git reference.
    """
    from ai.license import require_premium
    gate = require_premium("deploy_build")
    if gate:
        return gate
    from backends.deploy_bridge import build
    return _with_next_steps("deploy_build", _safe_call(build, app=app, git_ref=git_ref))


@mcp.tool()
def delimit_deploy_publish(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Publish images to registry (Pro).

    Args:
        app: Application name.
        git_ref: Git reference.
    """
    from ai.license import require_premium
    gate = require_premium("deploy_publish")
    if gate:
        return gate
    from backends.deploy_bridge import publish
    return _with_next_steps("deploy_publish", _safe_call(publish, app=app, git_ref=git_ref))


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_deploy_verify(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Verify deployment health (experimental) (Pro).

    Args:
        app: Application name.
        env: Target environment.
        git_ref: Git reference.
    """
    from ai.license import require_premium
    gate = require_premium("deploy_verify")
    if gate:
        return gate
    from backends.deploy_bridge import verify
    return _safe_call(verify, app=app, env=env, git_ref=git_ref)


@mcp.tool()
def delimit_deploy_rollback(app: str, env: str, to_sha: Optional[str] = None) -> Dict[str, Any]:
    """Rollback to previous SHA (Pro).

    Args:
        app: Application name.
        env: Target environment.
        to_sha: SHA to rollback to.
    """
    from ai.license import require_premium
    gate = require_premium("deploy_rollback")
    if gate:
        return gate
    from backends.deploy_bridge import rollback
    return _with_next_steps("deploy_rollback", _safe_call(rollback, app=app, env=env, to_sha=to_sha))


@mcp.tool()
def delimit_deploy_status(app: str, env: str) -> Dict[str, Any]:
    """Get deployment status (Pro).

    Args:
        app: Application name.
        env: Target environment.
    """
    from ai.license import require_premium
    gate = require_premium("deploy_status")
    if gate:
        return gate
    from backends.deploy_bridge import status
    return _with_next_steps("deploy_status", _safe_call(status, app=app, env=env))


# ─── Intel ──────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_intel_dataset_register(name: str, schema: Optional[Dict[str, Any]] = None, description: Optional[str] = None) -> Dict[str, Any]:
    """Register a new dataset in the file-based intel registry.

    Args:
        name: Dataset name.
        schema: Optional JSON schema for the dataset.
        description: Human-readable description.
    """
    from backends.tools_data import intel_dataset_register
    return _with_next_steps("intel_dataset_register", _safe_call(intel_dataset_register, name=name, schema=schema, description=description))


@mcp.tool()
def delimit_intel_dataset_list() -> Dict[str, Any]:
    """List all registered datasets from the intel registry."""
    from backends.tools_data import intel_dataset_list
    return _with_next_steps("intel_dataset_list", _safe_call(intel_dataset_list))


@mcp.tool()
def delimit_intel_dataset_freeze(dataset_id: str) -> Dict[str, Any]:
    """Mark a dataset as immutable (frozen). Prevents further modifications.

    Args:
        dataset_id: Dataset identifier.
    """
    from backends.tools_data import intel_dataset_freeze
    return _with_next_steps("intel_dataset_freeze", _safe_call(intel_dataset_freeze, dataset_id=dataset_id))


@mcp.tool()
def delimit_intel_snapshot_ingest(data: Dict[str, Any], provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Store a research snapshot with provenance metadata in the local intel store.

    Args:
        data: Snapshot data (any JSON-serializable dict).
        provenance: Optional provenance metadata (source, author, etc.).
    """
    from backends.tools_data import intel_snapshot_ingest
    return _with_next_steps("intel_snapshot_ingest", _safe_call(intel_snapshot_ingest, data=data, provenance=provenance))


@mcp.tool()
def delimit_intel_query(dataset_id: Optional[str] = None, query: str = "", parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Search saved intel snapshots by keyword, date, or dataset.

    Args:
        dataset_id: Optional dataset to filter by.
        query: Keyword search string.
        parameters: Optional params (date_from, date_to, limit).
    """
    from backends.tools_data import intel_query
    return _with_next_steps("intel_query", _safe_call(intel_query, dataset_id=dataset_id, query=query, parameters=parameters))


# ─── Generate ───────────────────────────────────────────────────────────

@mcp.tool()
def delimit_generate_template(template_type: str, name: str, framework: str = "nextjs", features: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate code template.

    Args:
        template_type: Template type (component, page, api, etc.).
        name: Name for the generated code.
        framework: Target framework.
        features: Optional feature flags.
    """
    from backends.generate_bridge import template
    return _with_next_steps("generate_template", _safe_call(template, template_type=template_type, name=name, framework=framework, features=features))


@mcp.tool()
def delimit_generate_scaffold(project_type: str, name: str, packages: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scaffold new project structure.

    Args:
        project_type: Project type (nextjs, api, library, etc.).
        name: Project name.
        packages: Packages to include.
    """
    from backends.generate_bridge import scaffold
    return _with_next_steps("generate_scaffold", _safe_call(scaffold, project_type=project_type, name=name, packages=packages))


# ─── Repo (RepoDoctor + ConfigSentry) ──────────────────────────────────

@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_repo_diagnose(target: str = ".") -> Dict[str, Any]:
    """Diagnose repository health issues (experimental) (Pro).

    Args:
        target: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("repo_diagnose")
    if gate:
        return gate
    from backends.repo_bridge import diagnose
    return _safe_call(diagnose, target=target)


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_repo_analyze(target: str = ".") -> Dict[str, Any]:
    """Analyze repository structure and quality (experimental) (Pro).

    Args:
        target: Repository path.
    """
    from ai.license import require_premium
    gate = require_premium("repo_analyze")
    if gate:
        return gate
    from backends.repo_bridge import analyze
    return _safe_call(analyze, target=target)


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_repo_config_validate(target: str = ".") -> Dict[str, Any]:
    """Validate configuration files (experimental) (Pro).

    Args:
        target: Repository or config path.
    """
    from ai.license import require_premium
    gate = require_premium("repo_config_validate")
    if gate:
        return gate
    from backends.repo_bridge import config_validate
    return _safe_call(config_validate, target=target)


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_repo_config_audit(target: str = ".") -> Dict[str, Any]:
    """Audit configuration compliance (experimental) (Pro).

    Args:
        target: Repository or config path.
    """
    from ai.license import require_premium
    gate = require_premium("repo_config_audit")
    if gate:
        return gate
    from backends.repo_bridge import config_audit
    return _safe_call(config_audit, target=target)


# ─── Security ───────────────────────────────────────────────────────────

@mcp.tool()
def delimit_security_scan(target: str = ".") -> Dict[str, Any]:
    """Scan for security vulnerabilities (Pro).

    Args:
        target: Repository or file path.
    """
    from ai.license import require_premium
    gate = require_premium("security_scan")
    if gate:
        return gate
    from backends.repo_bridge import security_scan
    return _with_next_steps("security_scan", _safe_call(security_scan, target=target))


@mcp.tool()
def delimit_security_audit(target: str = ".") -> Dict[str, Any]:
    """Audit security: dependency vulnerabilities, anti-patterns, and secret detection.

    Scans for:
    - Dependency vulnerabilities (pip-audit, npm audit)
    - Hardcoded secrets (API keys, tokens, passwords)
    - Dangerous patterns (eval, exec, SQL injection, XSS)
    - .env files tracked in git

    Optional: Set SNYK_TOKEN or install Trivy for enhanced scanning.

    Args:
        target: Repository or file path to audit.
    """
    from backends.tools_infra import security_audit
    return _with_next_steps("security_audit", _safe_call(security_audit, target=target))


# ─── Evidence ───────────────────────────────────────────────────────────

@mcp.tool()
def delimit_evidence_collect(target: str = ".") -> Dict[str, Any]:
    """Collect evidence artifacts for governance (Pro).

    Args:
        target: Repository or task path.
    """
    from ai.license import require_premium
    gate = require_premium("evidence_collect")
    if gate:
        return gate
    from backends.repo_bridge import evidence_collect
    return _with_next_steps("evidence_collect", _safe_call(evidence_collect, target=target))


@mcp.tool()
def delimit_evidence_verify(bundle_id: Optional[str] = None, bundle_path: Optional[str] = None) -> Dict[str, Any]:
    """Verify evidence bundle integrity (Pro).

    Args:
        bundle_id: Evidence bundle ID to verify.
        bundle_path: Path to evidence bundle file.
    """
    from ai.license import require_premium
    gate = require_premium("evidence_verify")
    if gate:
        return gate
    from backends.repo_bridge import evidence_verify
    return _with_next_steps("evidence_verify", _safe_call(evidence_verify, bundle_id=bundle_id, bundle_path=bundle_path))


# ═══════════════════════════════════════════════════════════════════════
#  TIER 4: OPS / UI — Governance Primitives + UI Tooling
# ═══════════════════════════════════════════════════════════════════════


# ─── ReleasePilot (Governance Primitive) ────────────────────────────────

@mcp.tool()
def delimit_release_plan(environment: str = "production", version: str = "", repository: str = ".", services: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate a release plan from git history.

    Reads git log since last tag, counts commits and changed files,
    suggests a semver version, and generates a release checklist.
    Saves plan to ~/.delimit/deploys/ for tracking.

    Args:
        environment: Target environment (staging/production).
        version: Release version (auto-detected if empty).
        repository: Repository path (default: current directory).
        services: Optional service list.
    """
    from backends.tools_infra import release_plan
    return _with_next_steps("release_plan", _safe_call(release_plan, environment=environment, version=version, repository=repository, services=services))


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_release_validate(environment: str, version: str) -> Dict[str, Any]:
    """Validate release readiness (experimental).

    Args:
        environment: Target environment.
        version: Release version.
    """
    from backends.ops_bridge import release_validate
    return _safe_call(release_validate, environment=environment, version=version)


@mcp.tool()
def delimit_release_status(environment: str = "production") -> Dict[str, Any]:
    """Check release/deploy status from file-based tracker and git state.

    Shows latest deploy plan, current git tag, how many commits HEAD
    is ahead of the tag, and recent deploy history.

    Args:
        environment: Target environment (staging/production).
    """
    from backends.tools_infra import release_status
    return _with_next_steps("release_status", _safe_call(release_status, environment=environment))


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_release_rollback(environment: str, version: str, to_version: str) -> Dict[str, Any]:
    """Rollback deployment to previous version (experimental).

    Args:
        environment: Target environment.
        version: Current version.
        to_version: Version to rollback to.
    """
    from backends.ops_bridge import release_rollback
    return _safe_call(release_rollback, environment=environment, version=version, to_version=to_version)


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_release_history(environment: str, limit: int = 10) -> Dict[str, Any]:
    """Show release history (experimental).

    Args:
        environment: Target environment.
        limit: Number of releases to return.
    """
    from backends.ops_bridge import release_history
    return _safe_call(release_history, environment=environment, limit=limit)


# ─── CostGuard (Governance Primitive) ──────────────────────────────────

@mcp.tool()
def delimit_cost_analyze(target: str = ".") -> Dict[str, Any]:
    """Analyze project costs by scanning Dockerfiles, dependencies, and cloud configs.

    Args:
        target: Project or infrastructure path to analyze.
    """
    from backends.tools_data import cost_analyze
    return _with_next_steps("cost_analyze", _safe_call(cost_analyze, target=target))


@mcp.tool()
def delimit_cost_optimize(target: str = ".") -> Dict[str, Any]:
    """Find cost optimization opportunities: unused deps, oversized images, uncompressed assets.

    Args:
        target: Project or infrastructure path to analyze.
    """
    from backends.tools_data import cost_optimize
    return _with_next_steps("cost_optimize", _safe_call(cost_optimize, target=target))


@mcp.tool()
def delimit_cost_alert(action: str = "list", name: Optional[str] = None,
                       threshold: Optional[float] = None, alert_id: Optional[str] = None) -> Dict[str, Any]:
    """Manage cost alerts (file-based). CRUD operations on spending thresholds.

    Args:
        action: Action (list/create/delete/toggle).
        name: Alert name (required for create).
        threshold: Cost threshold in USD (required for create).
        alert_id: Alert ID (required for delete/toggle).
    """
    from backends.tools_data import cost_alert
    return _with_next_steps("cost_alert", _safe_call(cost_alert, action=action, name=name, threshold=threshold, alert_id=alert_id))


# ─── DataSteward (Governance Primitive) ────────────────────────────────

@mcp.tool()
def delimit_data_validate(target: str = ".") -> Dict[str, Any]:
    """Validate data files: JSON parse, CSV structure, SQLite integrity check.

    Args:
        target: Directory or file path containing data files.
    """
    from backends.tools_data import data_validate
    return _with_next_steps("data_validate", _safe_call(data_validate, target=target))


@mcp.tool()
def delimit_data_migrate(target: str = ".") -> Dict[str, Any]:
    """Check for migration files (alembic, Django, Prisma, Knex) and report status.

    Args:
        target: Project path to scan for migration files.
    """
    from backends.tools_data import data_migrate
    return _with_next_steps("data_migrate", _safe_call(data_migrate, target=target))


@mcp.tool()
def delimit_data_backup(target: str = ".") -> Dict[str, Any]:
    """Back up SQLite and JSON data files to ~/.delimit/backups/ with timestamp.

    Args:
        target: Directory or file to back up.
    """
    from backends.tools_data import data_backup
    return _with_next_steps("data_backup", _safe_call(data_backup, target=target))


# ─── ObservabilityOps (Internal OS) ────────────────────────────────────

@mcp.tool()
def delimit_obs_metrics(query: str = "system", time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Query live system metrics (CPU, memory, disk I/O, network).

    Query types: cpu, memory, disk, io, network, system (default), all.
    Reads directly from /proc for real-time data.

    Optional: Set PROMETHEUS_URL for remote metrics.

    Args:
        query: Metrics query type (cpu|memory|disk|io|network|system|all).
        time_range: Time range (e.g. "1h", "24h", "7d").
        source: Optional metrics source (prometheus, local).
    """
    from backends.tools_infra import obs_metrics
    return _with_next_steps("obs_metrics", _safe_call(obs_metrics, query=query, time_range=time_range, source=source))


@mcp.tool()
def delimit_obs_logs(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Search system and application logs.

    Searches journalctl, /var/log/*, and application log directories.
    Returns matching log lines with source attribution.

    Optional: Set ELASTICSEARCH_URL or LOKI_URL for centralized log search.

    Args:
        query: Log search query string.
        time_range: Time range (5m, 15m, 1h, 6h, 24h, 7d).
        source: Log source path or integration name (journalctl, elasticsearch).
    """
    from backends.tools_infra import obs_logs
    return _with_next_steps("obs_logs", _safe_call(obs_logs, query=query, time_range=time_range, source=source))


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_obs_alerts(action: str, alert_rule: Optional[Dict[str, Any]] = None, rule_id: Optional[str] = None) -> Dict[str, Any]:
    """Manage alerting rules (experimental).

    Args:
        action: Action (list/create/delete/update).
        alert_rule: Alert rule definition (for create/update).
        rule_id: Rule ID (for delete/update).
    """
    from backends.ops_bridge import obs_alerts
    return _safe_call(obs_alerts, action=action, alert_rule=alert_rule, rule_id=rule_id)


@mcp.tool()
def delimit_obs_status() -> Dict[str, Any]:
    """System health check: disk space, memory, running services, uptime.

    Checks disk usage, memory, process count, load average, and probes
    common service ports (Node, PostgreSQL, Redis, Nginx, etc.).
    No external integration needed.
    """
    from backends.tools_infra import obs_status
    return _with_next_steps("obs_status", _safe_call(obs_status))


# ─── DesignSystem (UI Tooling) ──────────────────────────────────────────

@mcp.tool()
def delimit_design_extract_tokens(figma_file_key: Optional[str] = None, token_types: Optional[List[str]] = None, project_path: Optional[str] = None) -> Dict[str, Any]:
    """Extract design tokens from project CSS/SCSS/Tailwind config (or Figma if FIGMA_TOKEN set).

    Args:
        figma_file_key: Optional Figma file key (uses Figma API if FIGMA_TOKEN env var is set).
        token_types: Token types to extract (colors, typography, spacing, breakpoints).
        project_path: Project directory to scan. Defaults to cwd.
    """
    from backends.ui_bridge import design_extract_tokens
    return _with_next_steps("design_extract_tokens", _safe_call(design_extract_tokens, figma_file_key=figma_file_key, token_types=token_types, project_path=project_path))


@mcp.tool()
def delimit_design_generate_component(component_name: str, figma_node_id: Optional[str] = None, output_path: Optional[str] = None, project_path: Optional[str] = None) -> Dict[str, Any]:
    """Generate a React/Next.js component skeleton with props interface and Tailwind support.

    Args:
        component_name: Component name (PascalCase).
        figma_node_id: Optional Figma node ID (reserved for future use).
        output_path: Output file path. Defaults to components/<Name>/<Name>.tsx.
        project_path: Project root for Tailwind detection.
    """
    from backends.ui_bridge import design_generate_component
    return _with_next_steps("design_generate_component", _safe_call(design_generate_component, component_name=component_name, figma_node_id=figma_node_id, output_path=output_path, project_path=project_path))


@mcp.tool()
def delimit_design_generate_tailwind(figma_file_key: Optional[str] = None, output_path: Optional[str] = None, project_path: Optional[str] = None) -> Dict[str, Any]:
    """Read existing tailwind.config or generate one from detected CSS tokens.

    Args:
        figma_file_key: Optional Figma file key (reserved for future use).
        output_path: Output file path for generated config.
        project_path: Project root to scan for existing config or CSS tokens.
    """
    from backends.ui_bridge import design_generate_tailwind
    return _with_next_steps("design_generate_tailwind", _safe_call(design_generate_tailwind, figma_file_key=figma_file_key, output_path=output_path, project_path=project_path))


@mcp.tool()
def delimit_design_validate_responsive(project_path: str, check_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate responsive design patterns via static CSS analysis.

    Scans for media queries, viewport meta, mobile-first patterns, fixed widths.

    Args:
        project_path: Project path to validate.
        check_types: Check types (breakpoints, containers, fluid-type, etc.).
    """
    from backends.ui_bridge import design_validate_responsive
    return _with_next_steps("design_validate_responsive", _safe_call(design_validate_responsive, project_path=project_path, check_types=check_types))


@mcp.tool()
def delimit_design_component_library(project_path: str, output_format: str = "json") -> Dict[str, Any]:
    """Scan for React/Vue/Svelte components and generate a component catalog.

    Args:
        project_path: Project path to scan.
        output_format: Output format (json/markdown).
    """
    from backends.ui_bridge import design_component_library
    return _with_next_steps("design_component_library", _safe_call(design_component_library, project_path=project_path, output_format=output_format))


# ─── Story (Component Stories + Visual/A11y Testing) ────────────────────

@mcp.tool()
def delimit_story_generate(component_path: str, story_name: Optional[str] = None, variants: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate a .stories.tsx file for a component (no Storybook install required).

    Args:
        component_path: Path to the component file.
        story_name: Custom story name. Defaults to component name.
        variants: Variants to generate. Defaults to [Default, WithChildren].
    """
    from backends.ui_bridge import story_generate
    return _with_next_steps("story_generate", _safe_call(story_generate, component_path=component_path, story_name=story_name, variants=variants))


@mcp.tool()
def delimit_story_visual_test(url: str, project_path: Optional[str] = None, threshold: float = 0.05) -> Dict[str, Any]:
    """Run visual regression test -- screenshot with Playwright and compare to baseline.

    Falls back to guidance if Playwright is not installed.

    Args:
        url: URL to screenshot.
        project_path: Project path for baseline storage.
        threshold: Diff threshold (0.0-1.0).
    """
    from backends.ui_bridge import story_visual_test
    return _with_next_steps("story_visual_test", _safe_call(story_visual_test, url=url, project_path=project_path, threshold=threshold))


@_experimental_tool()  # HIDDEN: requires Storybook installed (LED-044)
def delimit_story_build(project_path: str, output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Build Storybook static site (requires Storybook installed).

    Args:
        project_path: Project path.
        output_dir: Output directory.
    """
    from backends.ui_bridge import story_build
    return _safe_call(story_build, project_path=project_path, output_dir=output_dir)


@mcp.tool()
def delimit_story_accessibility(project_path: str, standards: str = "WCAG2AA") -> Dict[str, Any]:
    """Run WCAG accessibility checks by scanning HTML/JSX/TSX for common issues.

    Checks: missing alt, missing labels, empty buttons, heading order, aria-hidden on focusable.

    Args:
        project_path: Project path to scan.
        standards: Accessibility standard (WCAG2A/WCAG2AA/WCAG2AAA).
    """
    from backends.ui_bridge import story_accessibility_test
    return _with_next_steps("story_accessibility", _safe_call(story_accessibility_test, project_path=project_path, standards=standards))


# ─── TestSmith (Testing — Real implementations) ──────────────────────

@mcp.tool()
def delimit_test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    """Generate test skeletons for source code.

    Scans source files using AST parsing (Python) or regex (JS/TS),
    extracts public function signatures, and generates test file skeletons.

    Args:
        project_path: Project path.
        source_files: Specific files to generate tests for.
        framework: Test framework (jest/pytest/vitest).
    """
    from backends.ui_bridge import test_generate
    return _with_next_steps("test_generate", _safe_call(test_generate, project_path=project_path, source_files=source_files, framework=framework))


@_experimental_tool()  # HIDDEN: stub/pass-through (LED-044)
def delimit_test_coverage(project_path: str, threshold: int = 80) -> Dict[str, Any]:
    """Analyze test coverage (experimental) (Pro).

    Args:
        project_path: Project path.
        threshold: Coverage threshold percentage.
    """
    from ai.license import require_premium
    gate = require_premium("test_coverage")
    if gate:
        return gate
    from backends.ui_bridge import test_coverage
    return _safe_call(test_coverage, project_path=project_path, threshold=threshold)


@mcp.tool()
def delimit_test_smoke(project_path: str, test_suite: Optional[str] = None) -> Dict[str, Any]:
    """Run smoke tests for a project.

    Detects the test framework (pytest/jest/vitest/mocha) from project config,
    runs the test suite, and parses pass/fail/error counts.

    Args:
        project_path: Project path.
        test_suite: Specific test suite to run.
    """
    from backends.ui_bridge import test_smoke
    return _with_next_steps("test_smoke", _safe_call(test_smoke, project_path=project_path, test_suite=test_suite))


# ─── Docs (Real implementations) ─────────────────────────────────────

@mcp.tool()
def delimit_docs_generate(target: str = ".") -> Dict[str, Any]:
    """Generate API reference documentation for a project.

    Scans Python files for docstrings and JS/TS files for JSDoc comments.
    Produces a markdown API reference organized by source file.

    Args:
        target: Project path.
    """
    from backends.ui_bridge import docs_generate
    return _with_next_steps("docs_generate", _safe_call(docs_generate, target=target))


@mcp.tool()
def delimit_docs_validate(target: str = ".") -> Dict[str, Any]:
    """Validate documentation quality and completeness.

    Checks README existence, docstring coverage on public functions,
    and broken internal links in markdown files.

    Args:
        target: Project path.
    """
    from backends.ui_bridge import docs_validate
    return _with_next_steps("docs_validate", _safe_call(docs_validate, target=target))




# ═══════════════════════════════════════════════════════════════════════
#  SENSING LAYER
# ═══════════════════════════════════════════════════════════════════════


# --- Sensing Layer (SENSE-002) ---

_NEGATIVE_KEYWORDS = [
    "not interested", "won't be", "will not", "don't need", "do not need",
    "no thanks", "pass on", "not a fit", "not for us", "closing",
    "won't adopt", "will not adopt", "reject", "declined",
]


@mcp.tool()
async def delimit_sensor_github_issue(
    repo: str,
    issue_number: int,
    since_comment_id: int = 0,
) -> Dict[str, Any]:
    """Check a GitHub issue for new comments since the last check.

    Sensor tool for monitoring outreach issues. Returns a structured signal
    with new comments, issue state, and severity classification.

    Args:
        repo: GitHub repository in owner/repo format (e.g. "activepieces/activepieces").
        issue_number: The issue number to monitor.
        since_comment_id: Last seen comment ID. Pass 0 to get all comments.
    """
    try:
        # Fetch comments
        comments_jq = (
            "[.[] | {id: .id, author: .user.login, "
            "created_at: .created_at, body: (.body | .[0:500])}]"
        )
        comments_proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{issue_number}/comments",
                "--jq", comments_jq,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if comments_proc.returncode != 0:
            return _with_next_steps("sensor_github_issue", {
                "error": f"gh api comments failed: {comments_proc.stderr.strip()}",
                "has_new_activity": False,
            })

        all_comments = json.loads(comments_proc.stdout) if comments_proc.stdout.strip() else []

        # Filter to new comments only
        new_comments = [c for c in all_comments if c["id"] > since_comment_id]

        # Fetch issue state
        issue_jq = "{state: .state, labels: [.labels[].name], reactions: .reactions.total_count}"
        issue_proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{issue_number}",
                "--jq", issue_jq,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if issue_proc.returncode != 0:
            return _with_next_steps("sensor_github_issue", {
                "error": f"gh api issue failed: {issue_proc.stderr.strip()}",
                "has_new_activity": False,
            })

        issue_info = json.loads(issue_proc.stdout) if issue_proc.stdout.strip() else {}
        issue_state = issue_info.get("state", "unknown")

        # Determine severity
        severity = "green"

        # Check for negative signals in new comments
        combined_body = " ".join(c.get("body", "") for c in new_comments).lower()
        has_negative = any(kw in combined_body for kw in _NEGATIVE_KEYWORDS)

        if has_negative:
            severity = "red"
        elif issue_state == "closed" and len(all_comments) == 0:
            # Closed with no engagement at all
            severity = "amber"
        elif issue_state == "closed":
            # Closed but had some engagement -- could be resolved or rejected
            severity = "amber"

        latest_comment_id = max((c["id"] for c in all_comments), default=since_comment_id)

        repo_key = repo.replace("/", "_")
        return _with_next_steps("sensor_github_issue", {
            "signal": {
                "id": f"sensor:github_issue:{repo_key}:{issue_number}",
                "venture": "delimit",
                "metric": "outreach_issue_activity",
                "source": f"https://github.com/{repo}/issues/{issue_number}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "severity": severity,
            },
            "issue_state": issue_state,
            "new_comments": new_comments,
            "latest_comment_id": latest_comment_id,
            "total_comments": len(all_comments),
            "has_new_activity": len(new_comments) > 0,
        })

    except subprocess.TimeoutExpired:
        return _with_next_steps("sensor_github_issue", {"error": "gh command timed out after 30s", "has_new_activity": False})
    except json.JSONDecodeError as e:
        return _with_next_steps("sensor_github_issue", {"error": f"Failed to parse gh output: {e}", "has_new_activity": False})
    except Exception as e:
        logger.error("Sensor error: %s\n%s", e, traceback.format_exc())
        return _with_next_steps("sensor_github_issue", {"error": str(e), "has_new_activity": False})


# ═══════════════════════════════════════════════════════════════════════
#  META
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
def delimit_version() -> Dict[str, Any]:
    """Return Delimit unified server version, all tiers, and tool count."""
    tiers = {
        "tier1_core": ["lint", "diff", "policy", "ledger", "impact", "semver", "explain", "zero_spec", "init"],
        "tier2_platform": [
            "os.plan", "os.status", "os.gates",
            "gov.health", "gov.status", "gov.policy", "gov.evaluate", "gov.new_task", "gov.run", "gov.verify",
            "memory.search", "memory.store", "memory.recent",
            "vault.search", "vault.health", "vault.snapshot",
        ],
        "tier3_extended": [
            "deploy.plan", "deploy.build", "deploy.publish", "deploy.verify", "deploy.rollback", "deploy.status",
            "intel.dataset_register", "intel.dataset_list", "intel.dataset_freeze", "intel.snapshot_ingest", "intel.query",
            "generate.template", "generate.scaffold",
            "repo.diagnose", "repo.analyze", "repo.config_validate", "repo.config_audit",
            "security.scan", "security.audit",
            "evidence.collect", "evidence.verify",
        ],
        "tier4_ops_ui": [
            "release.plan", "release.validate", "release.status", "release.rollback", "release.history",
            "cost.analyze", "cost.optimize", "cost.alert",
            "data.validate", "data.migrate", "data.backup",
            "obs.metrics", "obs.logs", "obs.alerts", "obs.status",
            "design.extract_tokens", "design.generate_component", "design.generate_tailwind", "design.validate_responsive", "design.component_library",
            "story.generate", "story.visual_test", "story.build", "story.accessibility",
            "test.generate", "test.coverage", "test.smoke",
            "docs.generate", "docs.validate",
        ],
        "sensing": [
            "sensor.github_issue",
        ],
    }
    total = sum(len(v) for v in tiers.values()) + 1  # +1 for version itself
    return _with_next_steps("version", {
        "version": VERSION,
        "server": "delimit-unified",
        "total_tools": total,
        "tiers": tiers,
        "adapter_contract": "v1.0",
        "authority": "delimit-gateway",
    })


# ═══════════════════════════════════════════════════════════════════════
#  META TOOLS (help, diagnose)
# ═══════════════════════════════════════════════════════════════════════


TOOL_HELP = {
    "init": {"desc": "Initialize governance for a project", "example": "delimit_init(project_path='.', preset='default')", "params": "project_path (str), preset (strict|default|relaxed)"},
    "lint": {"desc": "Diff two OpenAPI specs and check policy violations", "example": "delimit_lint(old_spec='base.yaml', new_spec='new.yaml')", "params": "old_spec (path), new_spec (path), policy_file (optional path)"},
    "diff": {"desc": "Pure diff between two specs — no policy, just changes", "example": "delimit_diff(old_spec='base.yaml', new_spec='new.yaml')", "params": "old_spec (path), new_spec (path)"},
    "semver": {"desc": "Classify the semver bump for a spec change", "example": "delimit_semver(old_spec='base.yaml', new_spec='new.yaml', current_version='1.2.3')", "params": "old_spec, new_spec, current_version (optional)"},
    "explain": {"desc": "Human-readable explanation of API changes", "example": "delimit_explain(old_spec='base.yaml', new_spec='new.yaml', template='pr_comment')", "params": "old_spec, new_spec, template (developer|pr_comment|migration|changelog)"},
    "gov_health": {"desc": "Check governance status — is the project initialized?", "example": "delimit_gov_health(repo='.')", "params": "repo (path, default '.')"},
    "test_coverage": {"desc": "Measure test coverage for a project", "example": "delimit_test_coverage(project_path='.', threshold=80)", "params": "project_path, threshold (default 80)"},
    "repo_analyze": {"desc": "Full repo health report — code quality, security, dependencies", "example": "delimit_repo_analyze(target='.')", "params": "target (path)"},
    "zero_spec": {"desc": "Extract OpenAPI spec from source code (FastAPI, Express, NestJS)", "example": "delimit_zero_spec(project_dir='.')", "params": "project_dir (path)"},
    "sensor_github_issue": {"desc": "Monitor a GitHub issue for new comments", "example": "delimit_sensor_github_issue(repo='owner/repo', issue_number=123)", "params": "repo (owner/name), issue_number (int)"},
}


STANDARD_WORKFLOWS = [
    {
        "name": "Project Onboarding",
        "description": "Set up governance for a new project",
        "steps": ["delimit_init", "delimit_gov_health", "delimit_lint", "delimit_test_coverage", "delimit_security_scan"],
    },
    {
        "name": "Pre-Commit Check",
        "description": "Validate changes before committing",
        "steps": ["delimit_lint", "delimit_test_coverage", "delimit_semver"],
    },
    {
        "name": "Security Audit",
        "description": "Full security scan with evidence collection",
        "steps": ["delimit_security_scan", "delimit_evidence_collect", "delimit_evidence_verify"],
    },
    {
        "name": "API Change Review",
        "description": "Review and document an API change",
        "steps": ["delimit_diff", "delimit_semver", "delimit_explain", "delimit_lint"],
    },
    {
        "name": "Deploy Pipeline",
        "description": "Build, publish, and verify a deployment",
        "steps": ["delimit_deploy_build", "delimit_deploy_publish", "delimit_deploy_verify"],
    },
]


@mcp.tool()
def delimit_help(tool_name: str = "") -> Dict[str, Any]:
    """Get help for a Delimit tool — what it does, parameters, and examples.

    Args:
        tool_name: Tool name (e.g. 'lint', 'gov_health'). Leave empty for overview.
    """
    if not tool_name:
        return _with_next_steps("help", {
            "message": "Delimit has 77 tools. Here are the most useful ones to start with:",
            "essential_tools": {k: v["desc"] for k, v in TOOL_HELP.items()},
            "workflows": STANDARD_WORKFLOWS,
            "tip": "Run delimit_help(tool_name='lint') for detailed help on a specific tool.",
            "all_tools": "Run delimit_version() for the complete list.",
        })

    # Normalize name
    clean = tool_name.replace("delimit_", "").replace("mcp__delimit__delimit_", "")
    info = TOOL_HELP.get(clean)
    if info:
        return _with_next_steps("help", {"tool": clean, **info})
    return _with_next_steps("help", {"error": f"No help for '{tool_name}'. Try: {', '.join(TOOL_HELP.keys())}"})


@mcp.tool()
def delimit_diagnose(project_path: str = ".") -> Dict[str, Any]:
    """Diagnose your Delimit setup — check environment, config, and tool status.

    Universal 'get me unstuck' command. Checks Python, MCP config, governance state,
    and reports any issues with suggested fixes.

    Args:
        project_path: Project to diagnose.
    """
    issues = []
    checks = {}

    # Python version
    import sys
    checks["python"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Check .delimit/ dir
    p = Path(project_path).resolve()
    delimit_dir = p / ".delimit"
    policies = delimit_dir / "policies.yml"
    ledger = delimit_dir / "ledger" / "events.jsonl"

    checks["project_path"] = str(p)
    checks["delimit_initialized"] = delimit_dir.is_dir()
    checks["policies_file"] = policies.is_file()
    checks["ledger_file"] = ledger.is_file()

    if not delimit_dir.is_dir():
        issues.append({
            "issue": "Project not initialized",
            "fix": "Run delimit_init(project_path='.') or say 'initialize governance for this project'",
        })
    elif not policies.is_file():
        issues.append({
            "issue": "Missing policies.yml",
            "fix": "Run delimit_init(project_path='.', preset='default')",
        })

    # Check key dependencies
    for pkg in ["yaml", "pydantic", "packaging"]:
        try:
            __import__(pkg)
            checks[f"dep_{pkg}"] = True
        except ImportError:
            checks[f"dep_{pkg}"] = False
            issues.append({"issue": f"Missing Python package: {pkg}", "fix": f"pip install {pkg}"})

    # Check fastmcp
    try:
        import fastmcp
        checks["fastmcp"] = True
    except ImportError:
        checks["fastmcp"] = False
        issues.append({"issue": "FastMCP not installed", "fix": "pip install fastmcp"})

    # Summary
    status = "healthy" if not issues else "issues_found"
    result = {
        "status": status,
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "tip": "If everything looks good but tools aren't working, try restarting Claude Code.",
    }
    # Dynamic next_steps: suggest init if not initialized
    diagnose_next = []
    if not delimit_dir.is_dir():
        diagnose_next.append({"tool": "delimit_init", "reason": "Initialize governance for this project", "suggested_args": {"preset": "default"}, "is_premium": False})
    result["next_steps"] = diagnose_next
    return result


# ═══════════════════════════════════════════════════════════════════════
#  LICENSE
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
def delimit_activate(license_key: str) -> Dict[str, Any]:
    """Activate a Delimit Pro license key.

    Args:
        license_key: The license key to activate (e.g. DELIMIT-XXXX-XXXX-XXXX).
    """
    from ai.license import activate_license
    return _with_next_steps("activate", activate_license(license_key))


@mcp.tool()
def delimit_license_status() -> Dict[str, Any]:
    """Check current Delimit license status -- tier, validity, and expiry."""
    from ai.license import get_license
    return _with_next_steps("license_status", get_license())


# ═══════════════════════════════════════════════════════════════════════
#  LEDGER (Strategy + Operational Task Tracking)
# ═══════════════════════════════════════════════════════════════════════


def _resolve_venture(venture: str) -> str:
    """Resolve a venture name or path to an actual directory path."""
    if not venture:
        return "."
    # If it's already a path
    if venture.startswith("/") or venture.startswith("~"):
        return str(Path(venture).expanduser())
    # Check registered ventures
    from ai.ledger_manager import list_ventures
    v = list_ventures()
    for name, info in v.get("ventures", {}).items():
        if name == venture or venture in name:
            return info.get("path", ".")
    # Fallback: assume it's a directory name under common roots
    for root in ["/home/delimit", "/home/jamsons/ventures", "/home"]:
        candidate = Path(root) / venture
        if candidate.exists():
            return str(candidate)
    return "."


@mcp.tool()
def delimit_ledger_add(
    title: str,
    venture: str = "",
    ledger: str = "ops",
    type: str = "task",
    priority: str = "P1",
    description: str = "",
    source: str = "session",
) -> Dict[str, Any]:
    """Add a new item to a project's ledger.

    The ledger tracks what needs to be done across sessions. Specify the venture/project
    name or path. If empty, auto-detects from current directory.

    Args:
        title: What needs to be done.
        venture: Project name or path (e.g. "delimit-gateway", "/home/delimit/delimit-gateway"). Auto-detects if empty.
        ledger: "ops" (tasks, bugs, features) or "strategy" (decisions, direction).
        type: task, fix, feat, strategy, consensus.
        priority: P0 (urgent), P1 (important), P2 (nice to have).
        description: Details.
        source: Where this came from (session, consensus, focus-group, etc).
    """
    from ai.ledger_manager import add_item
    project = _resolve_venture(venture)
    return add_item(title=title, ledger=ledger, type=type, priority=priority,
                    description=description, source=source, project_path=project)


@mcp.tool()
def delimit_ledger_done(item_id: str, note: str = "", venture: str = "") -> Dict[str, Any]:
    """Mark a ledger item as done.

    Args:
        item_id: The item ID (e.g. LED-001 or STR-001).
        note: Optional completion note.
        venture: Project name or path. Auto-detects if empty.
    """
    from ai.ledger_manager import update_item
    project = _resolve_venture(venture)
    return update_item(item_id=item_id, status="done", note=note, project_path=project)


@mcp.tool()
def delimit_ledger_list(
    venture: str = "",
    ledger: str = "both",
    status: str = "",
    priority: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    """List ledger items for a venture/project.

    Args:
        venture: Project name or path. Auto-detects if empty.
        ledger: "ops", "strategy", or "both".
        status: Filter by status — "open", "done", "in_progress", or empty for all.
        priority: Filter by priority — "P0", "P1", "P2", or empty for all.
        limit: Max items to return.
    """
    from ai.ledger_manager import list_items
    project = _resolve_venture(venture)
    return list_items(ledger=ledger, status=status or None, priority=priority or None, limit=limit, project_path=project)


@mcp.tool()
def delimit_ledger_context(venture: str = "") -> Dict[str, Any]:
    """Get a quick summary of what's open in the ledger.

    Auto-detects the venture from context. Pass a venture name to check a specific project.
    Returns the top 5 open items by priority so the AI knows what to work on.

    Args:
        venture: Project name or path. Auto-detects if empty.
    """
    from ai.ledger_manager import get_context
    project = _resolve_venture(venture) if venture else "."
    return get_context(project_path=project)


@mcp.tool()
def delimit_ventures() -> Dict[str, Any]:
    """List all registered ventures/projects that Delimit has been used with.

    Ventures are auto-registered when you use any Delimit tool in a project directory.
    """
    from ai.ledger_manager import list_ventures
    return list_ventures()


# ═══════════════════════════════════════════════════════════════════════
#  DELIBERATION (Multi-Round Consensus)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
def delimit_models(action: str = "list") -> Dict[str, Any]:
    """View and configure AI models for multi-model deliberation.

    Shows which models are available for consensus runs. Models auto-detect
    from environment variables (XAI_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY).

    Args:
        action: 'list' to show configured models.
    """
    from ai.deliberation import configure_models
    return configure_models()


@mcp.tool()
def delimit_deliberate(
    question: str,
    context: str = "",
    mode: str = "dialogue",
    max_rounds: int = 3,
    save_path: str = "",
) -> Dict[str, Any]:
    """Run multi-model consensus via real AI-to-AI deliberation.

    This is the consensus tool. Models (Grok 4, Gemini, Codex) debate each other
    directly until they reach unanimous agreement.

    Modes:
      - "dialogue": Short conversational turns like a group chat (default, 6 rounds)
      - "debate": Long-form essays with full counter-arguments (3 rounds)

    Args:
        question: The question to reach consensus on.
        context: Background context for all models.
        mode: "dialogue" (short turns) or "debate" (long essays).
        max_rounds: Maximum rounds (default 3 for debate, 6 for dialogue).
        save_path: Optional file path to save the full transcript.
    """
    from ai.deliberation import deliberate
    result = deliberate(
        question=question,
        context=context,
        mode=mode,
        max_rounds=max_rounds,
        save_path=save_path or "",
    )

    # Add summary for Claude to review
    rounds_count = len(result.get("rounds", []))
    unanimous = result.get("unanimous", False)

    summary = {
        "status": "unanimous" if unanimous else "no_consensus",
        "rounds": rounds_count,
        "agreed_at_round": result.get("agreed_at_round"),
        "final_verdict": result.get("final_verdict"),
        "transcript_saved": result.get("saved_to", save_path),
        "note": "Review the full transcript. As orchestrator, provide your own analysis and final synthesis.",
    }

    # Include last round responses for immediate review
    if result.get("rounds"):
        last_round = result["rounds"][-1]
        summary["gemini_final_response"] = last_round["responses"].get("gemini", "")[:2000]
        summary["grok_final_response"] = last_round["responses"].get("grok", "")[:2000]

    return summary


# ═══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

async def run_mcp_server(server, server_name="delimit"):
    """Run the MCP server."""
    await server.run_stdio_async()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_mcp_server(mcp))


def main():
    """Entry point for `delimit-mcp` console script."""
    import asyncio
    asyncio.run(run_mcp_server(mcp))
