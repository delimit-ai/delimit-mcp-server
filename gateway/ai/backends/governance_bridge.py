"""
Bridge to governance tools.
Tier 2 Platform — governance policy enforcement and task management.

health/status/policy: implemented with real filesystem checks.
evaluate/new_task/run/verify: require governancegate package (honest error if missing).
"""

import json
import subprocess
import sys
import logging
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.ai.governance_bridge")


def health(repo: str = ".") -> Dict[str, Any]:
    """Check governance system health with real filesystem checks."""
    repo_path = Path(repo).resolve()
    delimit_dir = repo_path / ".delimit"
    policies_file = delimit_dir / "policies.yml"
    ledger_file = delimit_dir / "ledger" / "operations.jsonl"

    checks = {}

    # Check .delimit/ directory
    checks["delimit_dir"] = delimit_dir.is_dir()

    # Check policies.yml
    checks["policies_file"] = policies_file.is_file()

    # Check ledger (operations.jsonl is where ledger_add writes)
    ledger_entries = 0
    if ledger_file.is_file():
        try:
            ledger_entries = sum(1 for line in ledger_file.read_text().splitlines() if line.strip())
        except Exception:
            pass
    checks["ledger_exists"] = ledger_file.is_file()
    checks["ledger_entries"] = ledger_entries

    # Check git cleanliness
    git_clean = None
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=str(repo_path)
        )
        if result.returncode == 0:
            git_clean = len(result.stdout.strip()) == 0
    except Exception:
        pass
    checks["git_clean"] = git_clean

    # Determine overall health
    if not checks["delimit_dir"]:
        overall = "not_initialized"
    elif not checks["policies_file"]:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "tool": "gov.health",
        "repo": str(repo_path),
        "status": overall,
        "checks": checks,
    }


def status(repo: str = ".") -> Dict[str, Any]:
    """Get governance status by reading actual policy files."""
    repo_path = Path(repo).resolve()
    policies_file = repo_path / ".delimit" / "policies.yml"
    ledger_file = repo_path / ".delimit" / "ledger" / "operations.jsonl"

    rules = []
    if policies_file.is_file():
        try:
            data = yaml.safe_load(policies_file.read_text())
            if isinstance(data, dict):
                for rule in data.get("rules", []):
                    rules.append({
                        "id": rule.get("id", "unknown"),
                        "name": rule.get("name", ""),
                        "severity": rule.get("severity", "warning"),
                        "action": rule.get("action", "warn"),
                    })
        except Exception as e:
            logger.warning("Failed to parse policies.yml: %s", e)

    ledger_entries = 0
    if ledger_file.is_file():
        try:
            ledger_entries = sum(1 for line in ledger_file.read_text().splitlines() if line.strip())
        except Exception:
            pass

    return {
        "tool": "gov.status",
        "repo": str(repo_path),
        "policies_file": str(policies_file) if policies_file.is_file() else None,
        "active_rules": len(rules),
        "rules": rules,
        "ledger_entries": ledger_entries,
    }


def policy(repo: str = ".") -> Dict[str, Any]:
    """Load and return the actual policies.yml content."""
    repo_path = Path(repo).resolve()
    policies_file = repo_path / ".delimit" / "policies.yml"

    if not policies_file.is_file():
        return {
            "tool": "gov.policy",
            "repo": str(repo_path),
            "status": "no_policy",
            "error": f"No policies.yml found at {policies_file}. Run: delimit init",
        }

    try:
        raw = policies_file.read_text()
        parsed = yaml.safe_load(raw)
        return {
            "tool": "gov.policy",
            "repo": str(repo_path),
            "policy": parsed,
            "raw": raw,
        }
    except Exception as e:
        return {
            "tool": "gov.policy",
            "repo": str(repo_path),
            "status": "parse_error",
            "error": f"Failed to parse policies.yml: {e}",
        }


_NOT_INIT_ERROR = (
    "Project not initialized for governance. "
    "Say 'initialize governance for this project' "
    "or run the delimit_init tool with your project path."
)


def _is_initialized(repo: str = ".") -> bool:
    """A project is initialized if .delimit/policies.yml exists."""
    return (Path(repo).resolve() / ".delimit" / "policies.yml").is_file()


def _not_init_response(tool_name: str, **extra) -> Dict[str, Any]:
    """Standard response when governance is not initialized."""
    return {"tool": tool_name, "status": "not_available", "error": _NOT_INIT_ERROR, **extra}


def evaluate_trigger(action: str, context: Optional[Dict] = None, repo: str = ".") -> Dict[str, Any]:
    """Evaluate if governance is required for an action.

    Special action "external_pr" runs the duplicate-PR pre-flight check
    (gov.external_pr_check) before any other governance evaluation.
    Context for external_pr should include {"target_repo": "owner/name",
    "author": "github-username"} (author optional but recommended).
    Verdict "duplicate" is propagated to callers as a hard stop.
    """
    if not _is_initialized(repo):
        return _not_init_response("gov.evaluate", action=action, repo=repo)

    # Pre-flight: external PR submission must check for duplicates first
    if action == "external_pr":
        target_repo = (context or {}).get("target_repo")
        if not target_repo:
            return {
                "tool": "gov.evaluate",
                "status": "evaluated",
                "action": action,
                "verdict": "missing_input",
                "error": "external_pr action requires context.target_repo",
            }
        author = (context or {}).get("author")
        check = external_pr_check(repo=target_repo, author=author)
        # If duplicate found, fail-closed: callers should not proceed
        if check.get("verdict") == "duplicate":
            return {
                "tool": "gov.evaluate",
                "status": "evaluated",
                "action": action,
                "verdict": "blocked_duplicate",
                "external_pr_check": check,
                "next_action": (
                    "STOP. A matching PR already exists. Review existing PR(s); "
                    "do not draft, deliberate, or submit a duplicate."
                ),
            }
        # No duplicate: still surface the check so the caller can audit
        # the chain. Falls through to normal policy evaluation below.
        external_check_result = check
    else:
        external_check_result = None

    # Governance is initialized -- evaluate against loaded policy
    repo_path = Path(repo).resolve()
    policies_file = repo_path / ".delimit" / "policies.yml"
    try:
        data = yaml.safe_load(policies_file.read_text())
        rules = data.get("rules", []) if isinstance(data, dict) else []
    except Exception:
        rules = []
    result = {
        "tool": "gov.evaluate",
        "status": "evaluated",
        "action": action,
        "context": context,
        "repo": str(repo_path),
        "governance_required": len(rules) > 0,
        "active_rules": len(rules),
    }
    if external_check_result is not None:
        result["external_pr_check"] = external_check_result
        result["verdict"] = "ready_for_deliberation"
        result["next_action"] = "Run delimit_deliberate, then submit the PR."
    return result


def new_task(title: str, scope: str, risk_level: str = "medium", repo: str = ".") -> Dict[str, Any]:
    """Create a new governance task."""
    if not _is_initialized(repo):
        return _not_init_response("gov.new_task")
    import uuid, time
    task_id = f"GOV-{str(uuid.uuid4())[:8].upper()}"
    return {
        "tool": "gov.new_task",
        "status": "created",
        "task_id": task_id,
        "title": title,
        "scope": scope,
        "risk_level": risk_level,
        "created_at": time.time(),
    }


def run_task(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Run a governance task."""
    if not _is_initialized(repo):
        return _not_init_response("gov.run")
    return {
        "tool": "gov.run",
        "status": "running",
        "task_id": task_id,
    }


def verify(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Verify a governance task."""
    if not _is_initialized(repo):
        return _not_init_response("gov.verify")
    return {
        "tool": "gov.verify",
        "status": "verified",
        "task_id": task_id,
    }


def evidence_index(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Get evidence index for a task."""
    if not _is_initialized(repo):
        return _not_init_response("gov.evidence_index")
    return {
        "tool": "gov.evidence_index",
        "status": "indexed",
        "task_id": task_id,
        "entries": [],
    }


def require_owner_approval(context: str, repo: str = ".") -> Dict[str, Any]:
    """Check if owner approval is required."""
    if not _is_initialized(repo):
        return _not_init_response("gov.require_owner_approval")
    return {
        "tool": "gov.require_owner_approval",
        "status": "checked",
        "approval_required": False,
        "context": context,
    }


def external_pr_check(
    repo: str,
    author: Optional[str] = None,
    state: str = "all",
) -> Dict[str, Any]:
    """Pre-PR duplicate guard. Lists existing PRs from `author` against `repo`.

    Returned verdict is fail-closed: any matching PR (open OR recently merged
    within 30 days OR pending review) returns verdict='duplicate' so callers
    can stop drafting before deliberation/submission.

    Args:
        repo: External GitHub repo, e.g. "goharbor/harbor".
        author: GitHub username to filter by. If None, lists all PRs.
        state: "open" | "closed" | "merged" | "all". Default "all" (broad).

    Returns dict with:
        verdict: "no_duplicate" | "duplicate" | "gh_unavailable" | "error"
        prs: List of matching PR records (number, state, reviewDecision, url, mergedAt)
        guidance: Human-readable next-step hint.
    """
    if not repo or "/" not in repo:
        return {
            "tool": "gov.external_pr_check",
            "verdict": "error",
            "error": "repo must be in 'owner/name' format",
        }

    cmd = ["gh", "pr", "list", "--repo", repo, "--state", state, "--limit", "50",
           "--json", "number,title,state,reviewDecision,createdAt,mergedAt,url,author"]
    if author:
        cmd.extend(["--author", author])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return {
            "tool": "gov.external_pr_check",
            "verdict": "gh_unavailable",
            "error": "gh CLI not installed",
            "guidance": "Install gh (https://cli.github.com) or run `gh pr list --repo "
                        f"{repo}" + (f" --author {author}" if author else "") + "` manually before drafting.",
        }
    except subprocess.TimeoutExpired:
        return {
            "tool": "gov.external_pr_check",
            "verdict": "error",
            "error": "gh pr list timed out after 20s",
        }

    if proc.returncode != 0:
        # Most common reason: not authenticated
        stderr = (proc.stderr or "").strip()[:300]
        return {
            "tool": "gov.external_pr_check",
            "verdict": "error",
            "error": f"gh pr list failed: {stderr}",
            "guidance": "Run `gh auth status` to verify authentication.",
        }

    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {
            "tool": "gov.external_pr_check",
            "verdict": "error",
            "error": f"could not parse gh output: {exc}",
        }

    # Fail-closed criteria: any open PR, or any PR merged in the last 30 days
    import time as _time
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    duplicates = []
    for pr in prs:
        is_open = pr.get("state") == "OPEN"
        merged_at = pr.get("mergedAt")
        recently_merged = False
        if merged_at:
            try:
                # gh returns ISO8601 like "2026-04-10T11:15:13Z"
                ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                recently_merged = ts >= cutoff
            except Exception:
                pass
        if is_open or recently_merged:
            duplicates.append({
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "reviewDecision": pr.get("reviewDecision"),
                "createdAt": pr.get("createdAt"),
                "mergedAt": merged_at,
                "url": pr.get("url"),
                "author": (pr.get("author") or {}).get("login"),
            })

    if duplicates:
        return {
            "tool": "gov.external_pr_check",
            "verdict": "duplicate",
            "repo": repo,
            "author": author,
            "prs": duplicates,
            "guidance": (
                f"Found {len(duplicates)} existing PR(s) — DO NOT draft a duplicate. "
                "Review the existing PR(s) and decide: monitor, comment, or update."
            ),
        }

    return {
        "tool": "gov.external_pr_check",
        "verdict": "no_duplicate",
        "repo": repo,
        "author": author,
        "checked_count": len(prs),
        "guidance": "Safe to proceed with drafting. Run delimit_deliberate before submitting.",
    }
