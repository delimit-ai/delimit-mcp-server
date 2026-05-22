"""LED-193 append-only execution audit log.

Every pickup attempt logs one JSON line — success or failure — so
incidents can be replayed against the daemon's actual behaviour.

Schema:
    {
        "ts": ISO8601 UTC,
        "item_id": str,
        "profile": "format_fix" | "lockfile_refresh" | "docs_typo" | "",
        "branch": str | "",          # auto/{profile}-{item_id}-{short_hash}
        "pr_url": str | "",          # populated only on success
        "result": "success" | "failed" | "noop" | "skipped" | "ci_failed_after_open",
        "reason": str,               # human-readable detail
        "cost_estimate": float,      # USD; 0.0 for deterministic profiles
        "files_changed": int,
        # optional, populated when known:
        "elapsed_s": float,
        "gate_results": dict,
    }

The log is append-only — never rewritten. Path:
    ``~/.delimit/led193_executions.jsonl``
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.ai.led193_daemon.audit")

AUDIT_LOG = Path.home() / ".delimit" / "led193_executions.jsonl"

VALID_RESULTS = {
    "success",
    "failed",
    "noop",
    "skipped",
    "ci_failed_after_open",
}


def log_execution(
    *,
    item_id: str,
    profile: str = "",
    branch: str = "",
    pr_url: str = "",
    result: str = "failed",
    reason: str = "",
    cost_estimate: float = 0.0,
    files_changed: int = 0,
    elapsed_s: Optional[float] = None,
    gate_results: Optional[Dict[str, Any]] = None,
    audit_log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write one append-only audit line.

    Returns the record actually written (useful for tests + the cron
    summary). Best-effort — a write failure logs a warning but never
    raises (the daemon must not crash on disk-full).
    """
    if result not in VALID_RESULTS:
        # Don't reject — coerce to "failed" with a clarifying reason so
        # we never silently drop an audit row over a typo in caller code.
        reason = f"invalid_result={result!r}; original_reason={reason!r}"
        result = "failed"

    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "item_id": item_id,
        "profile": profile,
        "branch": branch,
        "pr_url": pr_url,
        "result": result,
        "reason": reason,
        "cost_estimate": float(cost_estimate),
        "files_changed": int(files_changed),
    }
    if elapsed_s is not None:
        record["elapsed_s"] = round(float(elapsed_s), 3)
    if gate_results is not None:
        record["gate_results"] = gate_results

    target = audit_log_path or AUDIT_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort
        logger.warning("led193_daemon: failed to write audit log %s: %s", target, exc)

    return record


def recent_results(
    *,
    audit_log_path: Optional[Path] = None,
    limit: int = 100,
) -> list:
    """Read the most recent N records from the audit log (newest first).

    Used by the consecutive-failures circuit breaker and by the cron
    summary. Returns ``[]`` when the file doesn't exist or is empty.
    """
    target = audit_log_path or AUDIT_LOG
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out


def consecutive_failures(
    *,
    audit_log_path: Optional[Path] = None,
) -> int:
    """Count CONSECUTIVE failures from the most recent record backward.

    Stops at the first non-failure (success/noop/skipped). Used by the
    3-strike circuit breaker. ``ci_failed_after_open`` counts as a
    failure for breaker purposes — if the daemon keeps opening PRs that
    break CI, that's a signal to pause.
    """
    failures = 0
    for rec in recent_results(audit_log_path=audit_log_path, limit=20):
        if rec.get("result") in ("failed", "ci_failed_after_open"):
            failures += 1
            continue
        break
    return failures


def prs_opened_today(
    *,
    audit_log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """Count successful PRs opened in the last 24 hours.

    Used by the action-volume circuit breaker (max 5 PRs / day).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - 86400.0
    n = 0
    for rec in recent_results(audit_log_path=audit_log_path, limit=200):
        if rec.get("result") != "success" or not rec.get("pr_url"):
            continue
        ts = rec.get("ts") or ""
        try:
            rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if rec_dt.timestamp() >= cutoff:
                n += 1
        except (ValueError, TypeError):
            continue
    return n
