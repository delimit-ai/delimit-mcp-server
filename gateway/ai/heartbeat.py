"""Heartbeat liveness framework — Phase 1 local file-based (LED-1412).

Solves the silent-staleness class that the 2026-05-15 session exposed:
delimit-reddit-proxy.service was inactive/disabled for 13 days, all
reddit scans failed silently with 429/403, and the founder noticed via
"3 day old posts" — not the system. There was no central liveness
reporting and no alert.

Phase 1 (this module): every scheduled task writes a heartbeat file
when it runs. A central check tool walks the heartbeat directory and
flags anything stale. Local-only — Codex's correct caveat that
heartbeats can't catch a full-host outage motivates Phase 2 (external
deadman ping, tracked separately as LED-1414).

Heartbeat file format — one per service at ~/.delimit/heartbeats/<service>.json:
{
  "service": "delimit-reddit-proxy",
  "last_run": "2026-05-15T14:23:51Z",
  "last_success": "2026-05-15T14:23:51Z",  # may differ from last_run on partial failure
  "status": "ok" | "degraded" | "failed",
  "next_expected": "2026-05-15T15:23:51Z",
  "detail": "string — optional one-line context for status != ok"
}

Memory anchor: feedback_corrupted_worktree_phantom_failures.md (sister
failure class — both surface as "system reports stale data because no-one
checks freshness").
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# All heartbeats live under one directory. Override via env for tests.
DEFAULT_HEARTBEAT_DIR = Path.home() / ".delimit" / "heartbeats"

# Per-service staleness thresholds (seconds). Overridable via config file
# at ~/.delimit/heartbeats/_thresholds.json. Service names match the
# `service` key written by write_heartbeat().
DEFAULT_STALENESS_THRESHOLDS: Dict[str, int] = {
    # Reddit scanner: hourly social loop. >2 hours = stale.
    "delimit-reddit-proxy": 7200,
    "delimit-social-loop": 7200,
    # Inbox daemon: 5-min poll. >30 min = stale.
    "delimit-inbox": 1800,
    # License watch: daily timer. >36 hours = stale.
    "delimit-license-watch": 129600,
    # Drift check: daily. >36 hours = stale.
    "delimit-drift-check": 129600,
    # stake.one INJ-claim: daily 13:00 UTC. >30 hours = stale.
    "stakeone-inj-claim": 108000,
}

# Fallback for services not in the threshold map.
DEFAULT_FALLBACK_STALENESS = 86400  # 24 hours


def _heartbeat_dir(override: Optional[str] = None) -> Path:
    """Resolve the heartbeat directory. Honors:
    - explicit override arg
    - DELIMIT_HEARTBEAT_DIR env var
    - default ~/.delimit/heartbeats/
    """
    if override:
        return Path(override)
    env = os.environ.get("DELIMIT_HEARTBEAT_DIR")
    if env:
        return Path(env)
    return DEFAULT_HEARTBEAT_DIR


def _now_iso() -> str:
    """Current UTC time as ISO 8601 with Z suffix (matches existing
    delimit timestamp convention)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_iso(ts: str) -> Optional[float]:
    """Parse an ISO 8601 timestamp to a unix epoch float. Returns None
    on parse failure — callers treat None as 'unknown' (degraded but
    not actionable)."""
    if not ts:
        return None
    try:
        # %Y-%m-%dT%H:%M:%SZ — UTC, no fractional seconds.
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return None


def write_heartbeat(
    service: str,
    status: str = "ok",
    next_expected_in: Optional[int] = None,
    detail: str = "",
    success: bool = True,
    heartbeat_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a heartbeat for `service`.

    Called by every scheduled task at the end of its run. On success,
    pass status='ok' and success=True (default). On partial failure
    (e.g., one of N subreddits 429'd but most succeeded), pass
    status='degraded'. On total failure, status='failed' + success=False.

    Args:
        service: stable service identifier (e.g., 'delimit-reddit-proxy').
            Should match the systemd unit name where applicable.
        status: 'ok' | 'degraded' | 'failed'.
        next_expected_in: seconds until the next run is expected. Used
            by check_staleness to compute next_expected timestamp.
        detail: optional one-line context (printed to operators on stale).
        success: True if the run achieved its primary purpose (independent
            of `status` — a successful run can still be 'degraded' if
            some optional sub-tasks failed). last_success only updates
            when True.
        heartbeat_dir: override the heartbeat directory (for tests).

    Returns:
        Dict with the written record (also persisted to disk).
    """
    target_dir = _heartbeat_dir(heartbeat_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{service}.json"

    now = _now_iso()
    next_expected = ""
    if next_expected_in:
        next_expected_epoch = time.time() + next_expected_in
        next_expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_expected_epoch))

    # Preserve last_success across runs (only update if this run succeeded).
    last_success = now if success else ""
    if not success and file_path.exists():
        try:
            prior = json.loads(file_path.read_text())
            last_success = prior.get("last_success", "")
        except (json.JSONDecodeError, OSError):
            pass  # Ignore corrupted prior; treat as no last_success known.

    record = {
        "service": service,
        "last_run": now,
        "last_success": last_success,
        "status": status,
        "next_expected": next_expected,
        "detail": detail,
    }
    file_path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def read_heartbeats(heartbeat_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read every heartbeat file in the directory. Skips files that
    don't parse as JSON (corrupted heartbeats are reported as a separate
    'parse_error' entry so the operator sees them)."""
    target_dir = _heartbeat_dir(heartbeat_dir)
    if not target_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(target_dir.glob("*.json")):
        # Skip the threshold config file
        if path.name == "_thresholds.json":
            continue
        try:
            data = json.loads(path.read_text())
            out.append(data)
        except (json.JSONDecodeError, OSError) as e:
            out.append({
                "service": path.stem,
                "status": "parse_error",
                "detail": f"heartbeat file {path.name} unreadable: {type(e).__name__}: {e}",
                "last_run": "",
                "last_success": "",
                "next_expected": "",
            })
    return out


def _load_thresholds(heartbeat_dir: Optional[str] = None) -> Dict[str, int]:
    """Merge defaults with the optional override at <dir>/_thresholds.json."""
    thresholds = dict(DEFAULT_STALENESS_THRESHOLDS)
    target_dir = _heartbeat_dir(heartbeat_dir)
    override_path = target_dir / "_thresholds.json"
    if override_path.exists():
        try:
            override = json.loads(override_path.read_text())
            if isinstance(override, dict):
                thresholds.update({k: int(v) for k, v in override.items() if isinstance(v, (int, float))})
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return thresholds


def check_staleness(heartbeat_dir: Optional[str] = None) -> Dict[str, Any]:
    """Walk all heartbeats and classify each by staleness.

    Returns:
        {
          "checked_at": ISO8601 string,
          "summary": {"ok": N, "stale": N, "degraded": N, "failed": N, "parse_error": N},
          "services": [{service, status, last_run, last_success, age_seconds,
                        threshold_seconds, classification}],
          "stale_services": [<service names that are stale>],  # convenience for alerts
        }

    Classification rules (most-severe-first):
      - parse_error: heartbeat file unreadable
      - failed: status='failed' in the record
      - stale: last_run older than threshold
      - degraded: status='degraded' in the record
      - ok: status='ok' AND last_run within threshold
      - never_seen: heartbeat directory exists but service has no file
        (only reported when a service is configured in thresholds but
        has never written a heartbeat — surfaces "scheduled task never
        ran since heartbeat instrumentation landed")
    """
    now = time.time()
    records = read_heartbeats(heartbeat_dir)
    thresholds = _load_thresholds(heartbeat_dir)

    by_service: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        service = rec.get("service", "?unknown?")
        last_run_epoch = _parse_iso(rec.get("last_run", ""))
        threshold = thresholds.get(service, DEFAULT_FALLBACK_STALENESS)
        if last_run_epoch is not None:
            age_seconds = int(now - last_run_epoch)
        else:
            age_seconds = -1

        # Classify (most-severe-first)
        if rec.get("status") == "parse_error":
            classification = "parse_error"
        elif rec.get("status") == "failed":
            classification = "failed"
        elif age_seconds < 0:
            classification = "unknown_age"
        elif age_seconds > threshold:
            classification = "stale"
        elif rec.get("status") == "degraded":
            classification = "degraded"
        else:
            classification = "ok"

        by_service[service] = {
            "service": service,
            "status": rec.get("status", "?"),
            "last_run": rec.get("last_run", ""),
            "last_success": rec.get("last_success", ""),
            "age_seconds": age_seconds,
            "threshold_seconds": threshold,
            "classification": classification,
            "detail": rec.get("detail", ""),
        }

    # Add never_seen entries for configured services that have no record
    for service in thresholds.keys():
        if service not in by_service:
            by_service[service] = {
                "service": service,
                "status": "never_seen",
                "last_run": "",
                "last_success": "",
                "age_seconds": -1,
                "threshold_seconds": thresholds[service],
                "classification": "never_seen",
                "detail": "no heartbeat file found — service may not be instrumented yet",
            }

    services = list(by_service.values())
    summary = {"ok": 0, "stale": 0, "degraded": 0, "failed": 0, "parse_error": 0,
               "never_seen": 0, "unknown_age": 0}
    stale_services = []
    for svc in services:
        c = svc["classification"]
        summary[c] = summary.get(c, 0) + 1
        if c in ("stale", "failed", "parse_error", "never_seen"):
            stale_services.append(svc["service"])

    return {
        "checked_at": _now_iso(),
        "summary": summary,
        "services": services,
        "stale_services": stale_services,
    }
