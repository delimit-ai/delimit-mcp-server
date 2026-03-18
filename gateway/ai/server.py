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

VERSION = "2.0.0"


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
    return _safe_call(run_lint, old_spec=old_spec, new_spec=new_spec, policy_file=policy_file)


@mcp.tool()
def delimit_diff(old_spec: str, new_spec: str) -> Dict[str, Any]:
    """Diff two OpenAPI specs and list all changes. Pure diff, no policy.

    Args:
        old_spec: Path to the old OpenAPI spec file.
        new_spec: Path to the new OpenAPI spec file.
    """
    from backends.gateway_core import run_diff
    return _safe_call(run_diff, old_spec=old_spec, new_spec=new_spec)


@mcp.tool()
def delimit_policy(spec_files: List[str], policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Inspect or validate governance policy configuration.

    Args:
        spec_files: List of spec file paths.
        policy_file: Optional custom policy file path.
    """
    from backends.gateway_core import run_policy
    return _safe_call(run_policy, spec_files=spec_files, policy_file=policy_file)


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
    return _safe_call(query_ledger, ledger_path=ledger_path, api_name=api_name, repository=repository, validate_chain=validate_chain)


@mcp.tool()
def delimit_impact(api_name: str, dependency_file: Optional[str] = None) -> Dict[str, Any]:
    """Analyze downstream impact of an API change. Informational only.

    Args:
        api_name: The API that changed.
        dependency_file: Optional path to dependency manifest.
    """
    from backends.gateway_core import run_impact
    return _safe_call(run_impact, api_name=api_name, dependency_file=dependency_file)


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
    return _safe_call(run_semver, old_spec=old_spec, new_spec=new_spec, current_version=current_version)


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
    return _safe_call(run_explain, old_spec=old_spec, new_spec=new_spec, template=template, old_version=old_version, new_version=new_version, api_name=api_name)


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
    return _safe_call(run_zero_spec, project_dir=project_dir, python_bin=python_bin)




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
        return {
            "tool": "init",
            "status": "already_initialized",
            "project_path": str(root),
            "preset": preset,
            "message": f"Project already initialized at {delimit_dir}. No files overwritten.",
        }

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

    return {
        "tool": "init",
        "status": "initialized",
        "project_path": str(root),
        "preset": preset,
        "created": created,
        "message": f"Governance initialized with '{preset}' preset. {len(created)} items created.",
    }

# ═══════════════════════════════════════════════════════════════════════
#  TIER 2: PLATFORM — OS, Governance, Memory, Vault
# ═══════════════════════════════════════════════════════════════════════


# ─── OS ─────────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_os_plan(operation: str, target: str, parameters: Optional[Dict[str, Any]] = None, require_approval: bool = True) -> Dict[str, Any]:
    """Create a governed execution plan.

    Args:
        operation: Operation to plan (e.g. "deploy", "migrate").
        target: Target component or service.
        parameters: Operation parameters.
        require_approval: Whether to require approval before execution.
    """
    from backends.os_bridge import create_plan
    return _safe_call(create_plan, operation=operation, target=target, parameters=parameters, require_approval=require_approval)


@mcp.tool()
def delimit_os_status() -> Dict[str, Any]:
    """Get current Delimit OS status with plan/task/token counts."""
    from backends.os_bridge import get_status
    return _safe_call(get_status)


@mcp.tool()
def delimit_os_gates(plan_id: str) -> Dict[str, Any]:
    """Check governance gates for a plan.

    Args:
        plan_id: The plan ID (e.g. "PLAN-A1B2C3D4").
    """
    from backends.os_bridge import check_gates
    return _safe_call(check_gates, plan_id=plan_id)


# ─── Governance ─────────────────────────────────────────────────────────

@mcp.tool()
def delimit_gov_health(repo: str = ".") -> Dict[str, Any]:
    """Check governance system health.

    Args:
        repo: Repository path to check.
    """
    from backends.governance_bridge import health
    return _safe_call(health, repo=repo)


@mcp.tool()
def delimit_gov_status(repo: str = ".") -> Dict[str, Any]:
    """Get current governance status for a repository.

    Args:
        repo: Repository path.
    """
    from backends.governance_bridge import status
    return _safe_call(status, repo=repo)


@mcp.tool()
def delimit_gov_policy(repo: str = ".") -> Dict[str, Any]:
    """Get governance policy for a repository.

    Args:
        repo: Repository path.
    """
    from backends.governance_bridge import policy
    return _safe_call(policy, repo=repo)


@mcp.tool()
def delimit_gov_evaluate(action: str, context: Optional[Dict[str, Any]] = None, repo: str = ".") -> Dict[str, Any]:
    """Evaluate if governance is required for an action (requires governancegate).

    Args:
        action: The action to evaluate.
        context: Additional context.
        repo: Repository path.
    """
    from backends.governance_bridge import evaluate_trigger
    return _safe_call(evaluate_trigger, action=action, context=context, repo=repo)


@mcp.tool()
def delimit_gov_new_task(title: str, scope: str, risk_level: str = "medium", repo: str = ".") -> Dict[str, Any]:
    """Create a new governance task (requires governancegate).

    Args:
        title: Task title.
        scope: Task scope.
        risk_level: Risk level (low/medium/high/critical).
        repo: Repository path.
    """
    from backends.governance_bridge import new_task
    return _safe_call(new_task, title=title, scope=scope, risk_level=risk_level, repo=repo)


@mcp.tool()
def delimit_gov_run(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Run a governance task (requires governancegate).

    Args:
        task_id: Task ID to run.
        repo: Repository path.
    """
    from backends.governance_bridge import run_task
    return _safe_call(run_task, task_id=task_id, repo=repo)


@mcp.tool()
def delimit_gov_verify(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Verify a governance task (requires governancegate).

    Args:
        task_id: Task ID to verify.
        repo: Repository path.
    """
    from backends.governance_bridge import verify
    return _safe_call(verify, task_id=task_id, repo=repo)


# ─── Memory ─────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_memory_search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search conversation memory semantically.

    Args:
        query: Natural language search query.
        limit: Maximum results to return.
    """
    from backends.memory_bridge import search
    return _safe_call(search, query=query, limit=limit)


@mcp.tool()
def delimit_memory_store(content: str, tags: Optional[List[str]] = None, context: Optional[str] = None) -> Dict[str, Any]:
    """Store a memory entry for future retrieval.

    Args:
        content: The content to remember.
        tags: Optional categorization tags.
        context: Optional context about when/why this was stored.
    """
    from backends.memory_bridge import store
    return _safe_call(store, content=content, tags=tags, context=context)


@mcp.tool()
def delimit_memory_recent(limit: int = 5) -> Dict[str, Any]:
    """Get recent work summary from memory.

    Args:
        limit: Number of recent entries to return.
    """
    from backends.memory_bridge import get_recent
    return _safe_call(get_recent, limit=limit)


# ─── Vault ──────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_vault_search(query: str) -> Dict[str, Any]:
    """Search vault entries.

    Args:
        query: Search query for vault entries.
    """
    from backends.vault_bridge import search
    return _safe_call(search, query=query)


@mcp.tool()
def delimit_vault_health() -> Dict[str, Any]:
    """Check vault health status."""
    from backends.vault_bridge import health
    return _safe_call(health)


@mcp.tool()
def delimit_vault_snapshot() -> Dict[str, Any]:
    """Get a vault state snapshot."""
    from backends.vault_bridge import snapshot
    return _safe_call(snapshot)


# ═══════════════════════════════════════════════════════════════════════
#  TIER 3: EXTENDED — Deploy, Intel, Generate, Repo, Security, Evidence
# ═══════════════════════════════════════════════════════════════════════


# ─── Deploy ─────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_deploy_plan(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Plan deployment with build steps.

    Args:
        app: Application name.
        env: Target environment (staging/production).
        git_ref: Git reference (branch, tag, or SHA).
    """
    from backends.deploy_bridge import plan
    return _safe_call(plan, app=app, env=env, git_ref=git_ref)


@mcp.tool()
def delimit_deploy_build(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Build Docker images with SHA tags.

    Args:
        app: Application name.
        git_ref: Git reference.
    """
    from backends.deploy_bridge import build
    return _safe_call(build, app=app, git_ref=git_ref)


@mcp.tool()
def delimit_deploy_publish(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Publish images to registry.

    Args:
        app: Application name.
        git_ref: Git reference.
    """
    from backends.deploy_bridge import publish
    return _safe_call(publish, app=app, git_ref=git_ref)


@mcp.tool()
def delimit_deploy_verify(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Verify deployment health (experimental).

    Args:
        app: Application name.
        env: Target environment.
        git_ref: Git reference.
    """
    from backends.deploy_bridge import verify
    return _safe_call(verify, app=app, env=env, git_ref=git_ref)


@mcp.tool()
def delimit_deploy_rollback(app: str, env: str, to_sha: Optional[str] = None) -> Dict[str, Any]:
    """Rollback to previous SHA.

    Args:
        app: Application name.
        env: Target environment.
        to_sha: SHA to rollback to.
    """
    from backends.deploy_bridge import rollback
    return _safe_call(rollback, app=app, env=env, to_sha=to_sha)


@mcp.tool()
def delimit_deploy_status(app: str, env: str) -> Dict[str, Any]:
    """Get deployment status.

    Args:
        app: Application name.
        env: Target environment.
    """
    from backends.deploy_bridge import status
    return _safe_call(status, app=app, env=env)


# ─── Intel ──────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_intel_dataset_register(name: str, schema: Dict[str, Any], description: Optional[str] = None) -> Dict[str, Any]:
    """Register a new dataset with schema (coming soon).

    Args:
        name: Dataset name.
        schema: JSON schema for the dataset.
        description: Human-readable description.
    """
    from backends.intel_bridge import dataset_register
    return _safe_call(dataset_register, name=name, schema=schema, description=description)


@mcp.tool()
def delimit_intel_dataset_list() -> Dict[str, Any]:
    """List registered datasets (coming soon)."""
    from backends.intel_bridge import dataset_list
    return _safe_call(dataset_list)


@mcp.tool()
def delimit_intel_dataset_freeze(dataset_id: str) -> Dict[str, Any]:
    """Mark dataset as immutable (coming soon).

    Args:
        dataset_id: Dataset identifier.
    """
    from backends.intel_bridge import dataset_freeze
    return _safe_call(dataset_freeze, dataset_id=dataset_id)


@mcp.tool()
def delimit_intel_snapshot_ingest(data: Dict[str, Any], provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Store research snapshot with provenance (coming soon).

    Args:
        data: Snapshot data.
        provenance: Provenance metadata.
    """
    from backends.intel_bridge import snapshot_ingest
    return _safe_call(snapshot_ingest, data=data, provenance=provenance)


@mcp.tool()
def delimit_intel_query(dataset_id: str, query: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute deterministic query on dataset (coming soon).

    Args:
        dataset_id: Dataset to query.
        query: Query string.
        parameters: Query parameters.
    """
    from backends.intel_bridge import query_run
    return _safe_call(query_run, dataset_id=dataset_id, query=query, parameters=parameters)


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
    return _safe_call(template, template_type=template_type, name=name, framework=framework, features=features)


@mcp.tool()
def delimit_generate_scaffold(project_type: str, name: str, packages: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scaffold new project structure.

    Args:
        project_type: Project type (nextjs, api, library, etc.).
        name: Project name.
        packages: Packages to include.
    """
    from backends.generate_bridge import scaffold
    return _safe_call(scaffold, project_type=project_type, name=name, packages=packages)


# ─── Repo (RepoDoctor + ConfigSentry) ──────────────────────────────────

@mcp.tool()
def delimit_repo_diagnose(target: str = ".") -> Dict[str, Any]:
    """Diagnose repository health issues (experimental).

    Args:
        target: Repository path.
    """
    from backends.repo_bridge import diagnose
    return _safe_call(diagnose, target=target)


@mcp.tool()
def delimit_repo_analyze(target: str = ".") -> Dict[str, Any]:
    """Analyze repository structure and quality (experimental).

    Args:
        target: Repository path.
    """
    from backends.repo_bridge import analyze
    return _safe_call(analyze, target=target)


@mcp.tool()
def delimit_repo_config_validate(target: str = ".") -> Dict[str, Any]:
    """Validate configuration files (experimental).

    Args:
        target: Repository or config path.
    """
    from backends.repo_bridge import config_validate
    return _safe_call(config_validate, target=target)


@mcp.tool()
def delimit_repo_config_audit(target: str = ".") -> Dict[str, Any]:
    """Audit configuration compliance (experimental).

    Args:
        target: Repository or config path.
    """
    from backends.repo_bridge import config_audit
    return _safe_call(config_audit, target=target)


# ─── Security ───────────────────────────────────────────────────────────

@mcp.tool()
def delimit_security_scan(target: str = ".") -> Dict[str, Any]:
    """Scan for security vulnerabilities.

    Args:
        target: Repository or file path.
    """
    from backends.repo_bridge import security_scan
    return _safe_call(security_scan, target=target)


@mcp.tool()
def delimit_security_audit(target: str = ".") -> Dict[str, Any]:
    """Audit security compliance (experimental).

    Args:
        target: Repository or file path.
    """
    from backends.repo_bridge import security_audit
    return _safe_call(security_audit, target=target)


# ─── Evidence ───────────────────────────────────────────────────────────

@mcp.tool()
def delimit_evidence_collect(target: str = ".") -> Dict[str, Any]:
    """Collect evidence artifacts for governance.

    Args:
        target: Repository or task path.
    """
    from backends.repo_bridge import evidence_collect
    return _safe_call(evidence_collect, target=target)


@mcp.tool()
def delimit_evidence_verify(bundle_id: Optional[str] = None, bundle_path: Optional[str] = None) -> Dict[str, Any]:
    """Verify evidence bundle integrity.

    Args:
        bundle_id: Evidence bundle ID to verify.
        bundle_path: Path to evidence bundle file.
    """
    from backends.repo_bridge import evidence_verify
    return _safe_call(evidence_verify, bundle_id=bundle_id, bundle_path=bundle_path)


# ═══════════════════════════════════════════════════════════════════════
#  TIER 4: OPS / UI — Governance Primitives + UI Tooling
# ═══════════════════════════════════════════════════════════════════════


# ─── ReleasePilot (Governance Primitive) ────────────────────────────────

@mcp.tool()
def delimit_release_plan(environment: str, version: str, repository: str, services: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create deployment plan with approval gates (experimental).

    Args:
        environment: Target environment (staging/production).
        version: Release version.
        repository: Repository name.
        services: Optional service list.
    """
    from backends.ops_bridge import release_plan
    return _safe_call(release_plan, environment=environment, version=version, repository=repository, services=services)


@mcp.tool()
def delimit_release_validate(environment: str, version: str) -> Dict[str, Any]:
    """Validate release readiness (experimental).

    Args:
        environment: Target environment.
        version: Release version.
    """
    from backends.ops_bridge import release_validate
    return _safe_call(release_validate, environment=environment, version=version)


@mcp.tool()
def delimit_release_status(environment: str) -> Dict[str, Any]:
    """Check deployment status (experimental).

    Args:
        environment: Target environment.
    """
    from backends.ops_bridge import release_status
    return _safe_call(release_status, environment=environment)


@mcp.tool()
def delimit_release_rollback(environment: str, version: str, to_version: str) -> Dict[str, Any]:
    """Rollback deployment to previous version (experimental).

    Args:
        environment: Target environment.
        version: Current version.
        to_version: Version to rollback to.
    """
    from backends.ops_bridge import release_rollback
    return _safe_call(release_rollback, environment=environment, version=version, to_version=to_version)


@mcp.tool()
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
    """Analyze cost and spending patterns (experimental).

    Args:
        target: Project or infrastructure path.
    """
    from backends.ops_bridge import cost_analyze
    return _safe_call(cost_analyze, target=target)


@mcp.tool()
def delimit_cost_optimize(target: str = ".") -> Dict[str, Any]:
    """Generate cost optimization recommendations (experimental).

    Args:
        target: Project or infrastructure path.
    """
    from backends.ops_bridge import cost_optimize
    return _safe_call(cost_optimize, target=target)


@mcp.tool()
def delimit_cost_alert(action: str = "list") -> Dict[str, Any]:
    """Manage cost alerts and notifications (experimental).

    Args:
        action: Action (list/create/delete/update).
    """
    from backends.ops_bridge import cost_alert
    return _safe_call(cost_alert, action=action)


# ─── DataSteward (Governance Primitive) ────────────────────────────────

@mcp.tool()
def delimit_data_validate(target: str = ".") -> Dict[str, Any]:
    """Validate data integrity (experimental).

    Args:
        target: Data source or path.
    """
    from backends.ops_bridge import data_validate
    return _safe_call(data_validate, target=target)


@mcp.tool()
def delimit_data_migrate(target: str = ".") -> Dict[str, Any]:
    """Execute data migration (experimental, plan-only by default).

    Args:
        target: Data source or migration path.
    """
    from backends.ops_bridge import data_migrate
    return _safe_call(data_migrate, target=target)


@mcp.tool()
def delimit_data_backup(target: str = ".") -> Dict[str, Any]:
    """Create data backups (experimental).

    Args:
        target: Data source to back up.
    """
    from backends.ops_bridge import data_backup
    return _safe_call(data_backup, target=target)


# ─── ObservabilityOps (Internal OS) ────────────────────────────────────

@mcp.tool()
def delimit_obs_metrics(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Query and analyze metrics (experimental).

    Args:
        query: Metrics query.
        time_range: Time range (e.g. "1h", "24h", "7d").
        source: Optional metrics source.
    """
    from backends.ops_bridge import obs_metrics
    return _safe_call(obs_metrics, query=query, time_range=time_range, source=source)


@mcp.tool()
def delimit_obs_logs(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Query and search logs (experimental).

    Args:
        query: Log search query.
        time_range: Time range.
        source: Optional log source.
    """
    from backends.ops_bridge import obs_logs
    return _safe_call(obs_logs, query=query, time_range=time_range, source=source)


@mcp.tool()
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
    """Get observability system status (experimental)."""
    from backends.ops_bridge import obs_status
    return _safe_call(obs_status)


# ─── DesignSystem (UI Tooling) ──────────────────────────────────────────

@mcp.tool()
def delimit_design_extract_tokens(figma_file_key: str, token_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Extract design tokens from Figma (coming soon).

    Args:
        figma_file_key: Figma file key.
        token_types: Token types to extract (colors, typography, spacing, etc.).
    """
    from backends.ui_bridge import design_extract_tokens
    return _safe_call(design_extract_tokens, figma_file_key=figma_file_key, token_types=token_types)


@mcp.tool()
def delimit_design_generate_component(component_name: str, figma_node_id: Optional[str] = None, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Generate Next.js component from Figma design (coming soon).

    Args:
        component_name: Component name.
        figma_node_id: Figma node ID.
        output_path: Output file path.
    """
    from backends.ui_bridge import design_generate_component
    return _safe_call(design_generate_component, component_name=component_name, figma_node_id=figma_node_id, output_path=output_path)


@mcp.tool()
def delimit_design_generate_tailwind(figma_file_key: str, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Generate Tailwind config from Figma design tokens (coming soon).

    Args:
        figma_file_key: Figma file key.
        output_path: Output file path.
    """
    from backends.ui_bridge import design_generate_tailwind
    return _safe_call(design_generate_tailwind, figma_file_key=figma_file_key, output_path=output_path)


@mcp.tool()
def delimit_design_validate_responsive(project_path: str, check_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate responsive design patterns (coming soon).

    Args:
        project_path: Project path to validate.
        check_types: Check types (breakpoints, containers, fluid-type, etc.).
    """
    from backends.ui_bridge import design_validate_responsive
    return _safe_call(design_validate_responsive, project_path=project_path, check_types=check_types)


@mcp.tool()
def delimit_design_component_library(project_path: str, output_format: str = "json") -> Dict[str, Any]:
    """Generate component library documentation (coming soon).

    Args:
        project_path: Project path.
        output_format: Output format (json/markdown).
    """
    from backends.ui_bridge import design_component_library
    return _safe_call(design_component_library, project_path=project_path, output_format=output_format)


# ─── Storybook (UI Tooling + Visual Regression) ────────────────────────

@mcp.tool()
def delimit_story_generate(component_path: str, story_name: Optional[str] = None, variants: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate Storybook story for a component (coming soon).

    Args:
        component_path: Path to the component file.
        story_name: Custom story name.
        variants: Variants to generate.
    """
    from backends.ui_bridge import story_generate
    return _safe_call(story_generate, component_path=component_path, story_name=story_name, variants=variants)


@mcp.tool()
def delimit_story_visual_test(url: str, project_path: Optional[str] = None, threshold: float = 0.05) -> Dict[str, Any]:
    """Run visual regression test with Playwright screenshots (coming soon).

    Args:
        url: URL to test.
        project_path: Project path for baseline storage.
        threshold: Diff threshold (0.0-1.0).
    """
    from backends.ui_bridge import story_visual_test
    return _safe_call(story_visual_test, url=url, project_path=project_path, threshold=threshold)


@mcp.tool()
def delimit_story_build(project_path: str, output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Build Storybook static site (coming soon).

    Args:
        project_path: Project path.
        output_dir: Output directory.
    """
    from backends.ui_bridge import story_build
    return _safe_call(story_build, project_path=project_path, output_dir=output_dir)


@mcp.tool()
def delimit_story_accessibility(project_path: str, standards: str = "WCAG2AA") -> Dict[str, Any]:
    """Run WCAG accessibility tests on components (coming soon).

    Args:
        project_path: Project path.
        standards: Accessibility standard (WCAG2A/WCAG2AA/WCAG2AAA).
    """
    from backends.ui_bridge import story_accessibility_test
    return _safe_call(story_accessibility_test, project_path=project_path, standards=standards)


# ─── TestSmith (Testing) ───────────────────────────────────────────────

@mcp.tool()
def delimit_test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    """Generate tests for source code (experimental).

    Args:
        project_path: Project path.
        source_files: Specific files to generate tests for.
        framework: Test framework (jest/pytest/vitest).
    """
    from backends.ui_bridge import test_generate
    return _safe_call(test_generate, project_path=project_path, source_files=source_files, framework=framework)


@mcp.tool()
def delimit_test_coverage(project_path: str, threshold: int = 80) -> Dict[str, Any]:
    """Analyze test coverage (experimental).

    Args:
        project_path: Project path.
        threshold: Coverage threshold percentage.
    """
    from backends.ui_bridge import test_coverage
    return _safe_call(test_coverage, project_path=project_path, threshold=threshold)


@mcp.tool()
def delimit_test_smoke(project_path: str, test_suite: Optional[str] = None) -> Dict[str, Any]:
    """Run smoke tests (experimental).

    Args:
        project_path: Project path.
        test_suite: Specific test suite to run.
    """
    from backends.ui_bridge import test_smoke
    return _safe_call(test_smoke, project_path=project_path, test_suite=test_suite)


# ─── Docs ───────────────────────────────────────────────────────────────

@mcp.tool()
def delimit_docs_generate(target: str = ".") -> Dict[str, Any]:
    """Generate documentation for a project (experimental).

    Args:
        target: Project path.
    """
    from backends.ui_bridge import docs_generate
    return _safe_call(docs_generate, target=target)


@mcp.tool()
def delimit_docs_validate(target: str = ".") -> Dict[str, Any]:
    """Validate documentation quality and completeness (experimental).

    Args:
        target: Project path.
    """
    from backends.ui_bridge import docs_validate
    return _safe_call(docs_validate, target=target)




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
            return {
                "error": f"gh api comments failed: {comments_proc.stderr.strip()}",
                "has_new_activity": False,
            }

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
            return {
                "error": f"gh api issue failed: {issue_proc.stderr.strip()}",
                "has_new_activity": False,
            }

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
        return {
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
        }

    except subprocess.TimeoutExpired:
        return {"error": "gh command timed out after 30s", "has_new_activity": False}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse gh output: {e}", "has_new_activity": False}
    except Exception as e:
        logger.error("Sensor error: %s\n%s", e, traceback.format_exc())
        return {"error": str(e), "has_new_activity": False}


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
    return {
        "version": VERSION,
        "server": "delimit-unified",
        "total_tools": total,
        "tiers": tiers,
        "adapter_contract": "v1.0",
        "authority": "delimit-gateway",
    }


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


@mcp.tool()
def delimit_help(tool_name: str = "") -> Dict[str, Any]:
    """Get help for a Delimit tool — what it does, parameters, and examples.

    Args:
        tool_name: Tool name (e.g. 'lint', 'gov_health'). Leave empty for overview.
    """
    if not tool_name:
        return {
            "message": "Delimit has 77 tools. Here are the most useful ones to start with:",
            "essential_tools": {k: v["desc"] for k, v in TOOL_HELP.items()},
            "tip": "Run delimit_help(tool_name='lint') for detailed help on a specific tool.",
            "all_tools": "Run delimit_version() for the complete list.",
        }

    # Normalize name
    clean = tool_name.replace("delimit_", "").replace("mcp__delimit__delimit_", "")
    info = TOOL_HELP.get(clean)
    if info:
        return {"tool": clean, **info}
    return {"error": f"No help for '{tool_name}'. Try: {', '.join(TOOL_HELP.keys())}"}


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
    return {
        "status": status,
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "tip": "If everything looks good but tools aren't working, try restarting Claude Code.",
    }


# ═══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

async def run_mcp_server(server, server_name="delimit"):
    """Run the MCP server."""
    await server.run_stdio_async()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_mcp_server(mcp))
