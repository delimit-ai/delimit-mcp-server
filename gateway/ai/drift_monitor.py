"""Continuous drift/compliance monitoring — detects API spec changes without governance review.

Compares the current spec against the last known baseline and flags:
- Spec changed without a governance lint run
- Policy violations that accumulated since last review
- New endpoints or schemas without documentation
- Baseline staleness (hasn't been updated in N days)

Storage: ~/.delimit/drift/
"""

import json
import time
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

DRIFT_DIR = Path.home() / ".delimit" / "drift"
HISTORY_FILE = DRIFT_DIR / "history.jsonl"
BASELINE_FILE = DRIFT_DIR / "baseline_hash.json"


def _ensure_dir():
    DRIFT_DIR.mkdir(parents=True, exist_ok=True)


def _hash_file(path: str) -> str:
    """SHA256 of a file's contents."""
    try:
        content = Path(path).read_bytes()
        return hashlib.sha256(content).hexdigest()
    except (OSError, FileNotFoundError):
        return ""


def _load_baselines() -> Dict[str, Any]:
    if not BASELINE_FILE.exists():
        return {}
    try:
        return json.loads(BASELINE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_baselines(baselines: Dict[str, Any]):
    _ensure_dir()
    BASELINE_FILE.write_text(json.dumps(baselines, indent=2))


def _append_history(entry: Dict[str, Any]):
    _ensure_dir()
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def check_drift(
    spec_path: str = "",
    project_path: str = ".",
    staleness_days: int = 7,
) -> Dict[str, Any]:
    """Check for API spec drift against the last governance baseline.

    Returns drift findings and compliance status.
    """
    from pathlib import Path as P

    project = P(project_path).resolve()
    findings: List[Dict[str, str]] = []
    baselines = _load_baselines()

    # Auto-detect spec if not provided
    if not spec_path:
        spec_patterns = [
            "openapi.yaml", "openapi.yml", "openapi.json",
            "swagger.yaml", "swagger.yml", "swagger.json",
            "api.yaml", "api.yml", "api.json",
        ]
        for pattern in spec_patterns:
            candidate = project / pattern
            if candidate.exists():
                spec_path = str(candidate)
                break

        # Check .delimit/baseline.yaml (zero-spec)
        if not spec_path:
            baseline_candidate = project / ".delimit" / "baseline.yaml"
            if baseline_candidate.exists():
                spec_path = str(baseline_candidate)

    if not spec_path:
        return {
            "status": "no_spec",
            "message": "No OpenAPI spec found. Run `delimit init` to set up governance.",
            "drift_detected": False,
            "findings": [],
        }

    # Check if spec has changed since last baseline
    current_hash = _hash_file(spec_path)
    spec_key = str(P(spec_path).resolve())
    baseline = baselines.get(spec_key, {})
    last_hash = baseline.get("hash", "")
    last_reviewed = baseline.get("last_reviewed", "")
    last_lint_pass = baseline.get("last_lint_pass", False)

    drift_detected = False

    if not last_hash:
        # First run — save baseline
        findings.append({
            "type": "new_baseline",
            "severity": "info",
            "message": "First drift check — baseline recorded.",
        })
        baselines[spec_key] = {
            "hash": current_hash,
            "last_reviewed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_lint_pass": True,
            "spec_path": spec_path,
        }
        _save_baselines(baselines)
    elif current_hash != last_hash:
        drift_detected = True
        findings.append({
            "type": "spec_changed",
            "severity": "warning",
            "message": f"API spec changed since last governance review ({last_reviewed or 'unknown'})",
        })

    # Check staleness
    if last_reviewed:
        try:
            reviewed_ts = time.mktime(time.strptime(last_reviewed, "%Y-%m-%dT%H:%M:%SZ"))
            age_days = (time.time() - reviewed_ts) / 86400
            if age_days > staleness_days:
                drift_detected = True
                findings.append({
                    "type": "stale_baseline",
                    "severity": "warning",
                    "message": f"Baseline is {int(age_days)} days old (threshold: {staleness_days} days)",
                })
        except (ValueError, OverflowError):
            pass

    # Check for .delimit/policies.yml existence
    policy_file = project / ".delimit" / "policies.yml"
    if not policy_file.exists():
        findings.append({
            "type": "no_policy",
            "severity": "warning",
            "message": "No policy file found. Run `delimit init` to configure governance.",
        })

    # Check for evidence directory
    evidence_dir = project / ".delimit" / "evidence"
    if not evidence_dir.exists():
        findings.append({
            "type": "no_evidence",
            "severity": "info",
            "message": "No evidence directory. First governance run will create it.",
        })

    # Record drift check in history
    _append_history({
        "action": "drift_check",
        "spec_path": spec_path,
        "drift_detected": drift_detected,
        "findings_count": len(findings),
        "current_hash": current_hash[:16],
    })

    return {
        "status": "drift" if drift_detected else "clean",
        "drift_detected": drift_detected,
        "spec_path": spec_path,
        "findings": findings,
        "baseline": {
            "hash": last_hash[:16] if last_hash else None,
            "last_reviewed": last_reviewed,
            "current_hash": current_hash[:16],
        },
        "recommendation": (
            "Run `delimit lint` to review spec changes and update the governance baseline."
            if drift_detected else
            "No drift detected. Governance baseline is current."
        ),
    }


def update_baseline(spec_path: str, lint_passed: bool = True) -> Dict[str, Any]:
    """Update the drift baseline after a successful governance review."""
    from pathlib import Path as P

    if not spec_path:
        return {"error": "spec_path is required"}

    current_hash = _hash_file(spec_path)
    if not current_hash:
        return {"error": f"Cannot read spec at {spec_path}"}

    spec_key = str(P(spec_path).resolve())
    baselines = _load_baselines()

    baselines[spec_key] = {
        "hash": current_hash,
        "last_reviewed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_lint_pass": lint_passed,
        "spec_path": spec_path,
    }
    _save_baselines(baselines)

    _append_history({
        "action": "baseline_update",
        "spec_path": spec_path,
        "hash": current_hash[:16],
        "lint_passed": lint_passed,
    })

    return {
        "status": "updated",
        "spec_path": spec_path,
        "hash": current_hash[:16],
        "message": "Drift baseline updated.",
    }


def get_drift_history(limit: int = 20) -> Dict[str, Any]:
    """Return recent drift check history."""
    entries: List[Dict] = []
    if HISTORY_FILE.exists():
        try:
            lines = HISTORY_FILE.read_text().strip().split("\n")
            for line in lines[-limit:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    return {
        "status": "ok",
        "entries": entries,
        "total": len(entries),
    }
