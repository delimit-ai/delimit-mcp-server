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
    ledger_file = delimit_dir / "ledger" / "events.jsonl"

    checks = {}

    # Check .delimit/ directory
    checks["delimit_dir"] = delimit_dir.is_dir()

    # Check policies.yml
    checks["policies_file"] = policies_file.is_file()

    # Check ledger
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
    ledger_file = repo_path / ".delimit" / "ledger" / "events.jsonl"

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


def evaluate_trigger(action: str, context: Optional[Dict] = None, repo: str = ".") -> Dict[str, Any]:
    """Evaluate if governance is required for an action."""
    return {
        "tool": "gov.evaluate",
        "status": "not_available",
        "error": "Governance evaluation engine not running. This tool requires the governancegate package.",
        "action": action,
        "repo": repo,
    }


def new_task(title: str, scope: str, risk_level: str = "medium", repo: str = ".") -> Dict[str, Any]:
    """Create a new governance task."""
    return {
        "tool": "gov.new_task",
        "status": "not_available",
        "error": "Governance task engine not running. This tool requires the governancegate package.",
    }


def run_task(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Run a governance task."""
    return {
        "tool": "gov.run",
        "status": "not_available",
        "error": "Governance task engine not running. This tool requires the governancegate package.",
    }


def verify(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Verify a governance task."""
    return {
        "tool": "gov.verify",
        "status": "not_available",
        "error": "Governance task engine not running. This tool requires the governancegate package.",
    }


def evidence_index(task_id: str, repo: str = ".") -> Dict[str, Any]:
    """Get evidence index for a task."""
    return {
        "tool": "gov.evidence_index",
        "status": "not_available",
        "error": "Governance evidence engine not running. This tool requires the governancegate package.",
    }


def require_owner_approval(context: str, repo: str = ".") -> Dict[str, Any]:
    """Check if owner approval is required."""
    return {
        "tool": "gov.require_owner_approval",
        "status": "not_available",
        "error": "Governance approval engine not running. This tool requires the governancegate package.",
    }
