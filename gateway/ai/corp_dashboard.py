"""Corp dashboard — single-call corp-status synthesis (LED-189).

Replaces the 6-call session-start ritual with one MCP tool.

Aggregates state across:
  - systemd units (delimit-self-repair, delimit-inbox, delimit-social-loop)
  - self-repair daemon (in-memory + persisted state)
  - inbox daemon (in-memory state)
  - social daemon (in-memory state) + recent draft/post activity
  - ledger (open / in-progress / P0 counts)
  - agent queue (audit-only counts from ~/.delimit/agents/tasks.json)
  - latest session file (last revive timestamp + soul id)

Every sub-section is wrapped in try/except — a partial failure returns
{"error": "<reason>"} for that key only and does NOT crash the dashboard.

Gateway-only module. Excluded from npm bundle via package.json files-array.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Paths (lazy to allow monkeypatching) ─────────────────────────────


def _delimit_home() -> Path:
    """Return ~/.delimit. Honours DELIMIT_HOME env override for tests."""
    override = os.environ.get("DELIMIT_HOME")
    if override:
        return Path(override)
    return Path.home() / ".delimit"


# Systemd units that back the corp daemons. The unit name is the
# ground truth for "is this daemon supposed to be running on this box?";
# the in-process daemon state object only knows about the current MCP
# server process.
SYSTEMD_UNITS = (
    "delimit-self-repair",
    "delimit-inbox",
    "delimit-social-loop",
    "delimit-daemon",
)


# ── Helpers ───────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _systemctl_available() -> bool:
    """Best-effort detection of systemctl.

    Returns False on macOS, in containers without systemd, or anywhere
    `systemctl` is missing from $PATH. Callers MUST handle False —
    reporting `available: False` rather than failing the whole dashboard.
    """
    return shutil.which("systemctl") is not None


def _systemd_unit_status(unit: str) -> Dict[str, Any]:
    """Return {active: bool, status_str: str} for a systemd unit.

    Uses `systemctl is-active <unit>` which exits 0 when active and
    non-zero otherwise. The stdout string is the canonical state name
    ("active", "inactive", "failed", "activating", "unknown", etc.).

    On systems without systemd, returns active=False and status_str
    set to "systemd_unavailable" so the caller can distinguish "daemon
    not running" from "this box doesn't have systemd at all".
    """
    if not _systemctl_available():
        return {"active": False, "status_str": "systemd_unavailable"}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        status_str = (result.stdout or result.stderr or "").strip() or "unknown"
        # is-active returns 0 only when fully active. activating/reloading
        # exit non-zero but stdout still tells us the real state.
        return {"active": result.returncode == 0, "status_str": status_str}
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("systemctl is-active %s failed: %s", unit, exc)
        return {"active": False, "status_str": f"error: {exc}"}


def _read_jsonl(path: Path, *, since: Optional[datetime] = None) -> list[Dict[str, Any]]:
    """Read a jsonl file. Optionally filter rows newer than `since` based
    on the row's `ts` or `timestamp` field. Malformed lines are skipped."""
    if not path.exists():
        return []
    rows: list[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None:
                    ts_raw = row.get("ts") or row.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if ts < since:
                        continue
                rows.append(row)
    except OSError as exc:
        logger.warning("corp_dashboard: cannot read %s: %s", path, exc)
    return rows


# ── Sub-section collectors ────────────────────────────────────────────


def _collect_systemd() -> Dict[str, Any]:
    """Per-unit systemd status. Each key is the unit short-name."""
    out: Dict[str, Any] = {"available": _systemctl_available()}
    for unit in SYSTEMD_UNITS:
        out[unit] = _systemd_unit_status(unit)
    # Convenience aggregate: how many of the watched units are active?
    active_count = sum(1 for u in SYSTEMD_UNITS if out[u].get("active"))
    out["active_count"] = active_count
    out["total_count"] = len(SYSTEMD_UNITS)
    return out


def _collect_self_repair() -> Dict[str, Any]:
    """Self-repair daemon: persisted state file is canonical (the systemd-
    managed daemon is the sole writer). Live in-process status is only a
    fallback when persisted is missing — relevant in dev/test, not prod.

    Earlier the precedence was reversed (live first), which produced false
    "running: false, total_passes: 0" reports when the dashboard ran in a
    subprocess that wasn't the daemon's process. Persisted file wins.
    """
    state_path = _delimit_home() / "self_repair_daemon_state.json"
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            return {
                "running": bool(snap.get("running")),
                "last_pass": snap.get("last_pass"),
                "total_passes": int(snap.get("total_passes", 0) or 0),
                "breaches_emitted": int(snap.get("breaches_emitted", 0) or 0),
                "consecutive_failures": int(snap.get("consecutive_failures", 0) or 0),
                "poll_interval_seconds": int(snap.get("poll_interval_seconds", 0) or 0),
                "paused_env": bool(snap.get("paused_env", False)),
                "source": "persisted",
            }
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": f"state file unreadable: {exc}"}

    # No persisted file — try live (only meaningful in same-process scenarios).
    try:
        from ai.self_repair_daemon import get_daemon_status as _sr_status
        live = _sr_status()
        return {
            "running": bool(live.get("running")),
            "last_pass": live.get("last_pass"),
            "total_passes": int(live.get("total_passes", 0) or 0),
            "breaches_emitted": int(live.get("breaches_emitted", 0) or 0),
            "consecutive_failures": int(live.get("consecutive_failures", 0) or 0),
            "poll_interval_seconds": int(live.get("poll_interval_seconds", 0) or 0),
            "paused_env": bool(live.get("paused_env", False)),
            "source": "live",
        }
    except Exception as exc:
        logger.debug("self_repair fallback to live failed: %s", exc)

    return {"error": "no persisted state file and no live daemon"}


def _collect_inbox() -> Dict[str, Any]:
    """Inbox daemon: running flag + last poll + pending approvals count."""
    from ai.inbox_daemon import get_daemon_status as _inbox_status  # lazy

    raw = _inbox_status()
    pending = raw.get("pending_approvals", []) or []
    return {
        "running": bool(raw.get("running")),
        "last_poll": raw.get("last_poll"),
        "items_processed": int(raw.get("items_processed", 0) or 0),
        "pending_approvals": len(pending) if isinstance(pending, list) else 0,
    }


def _collect_social() -> Dict[str, Any]:
    """Social daemon state + 24h draft / post activity counters."""
    from ai.social_daemon import get_daemon_status as _social_status  # lazy

    raw = _social_status()
    out: Dict[str, Any] = {
        "running": bool(raw.get("running")),
        "last_scan": raw.get("last_scan"),
        "targets_found": int(raw.get("targets_found", 0) or 0),
    }

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    home = _delimit_home()
    try:
        drafts = _read_jsonl(home / "social_drafts.jsonl", since=since)
        out["drafts_24h"] = len(drafts)
        # Latest draft timestamp (any row, regardless of since-filter).
        all_drafts = _read_jsonl(home / "social_drafts.jsonl")
        if all_drafts:
            last_ts = None
            for row in reversed(all_drafts):
                last_ts = row.get("timestamp") or row.get("ts")
                if last_ts:
                    break
            out["last_draft_ts"] = last_ts
        else:
            out["last_draft_ts"] = None
    except Exception as exc:
        out["drafts_24h"] = 0
        out["last_draft_ts"] = None
        out["drafts_error"] = str(exc)

    try:
        posts = _read_jsonl(home / "social_log.jsonl", since=since)
        out["posts_24h"] = len(posts)
    except Exception as exc:
        out["posts_24h"] = 0
        out["posts_error"] = str(exc)

    return out


def _collect_ledger() -> Dict[str, Any]:
    """Open ledger items grouped by status + P0 pending count.

    Scans ~/.delimit/ledger/*.jsonl. Each line is an item with at minimum
    `status` and `priority`. Tolerant of malformed lines.
    """
    ledger_dir = _delimit_home() / "ledger"
    if not ledger_dir.exists():
        return {"open_count": 0, "in_progress_count": 0, "p0_pending": 0}

    open_count = 0
    in_progress_count = 0
    p0_pending = 0
    total_scanned = 0

    for jsonl_path in ledger_dir.glob("*.jsonl"):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total_scanned += 1
                    status = (row.get("status") or "").lower()
                    if status == "open":
                        open_count += 1
                    elif status in ("in_progress", "in-progress", "active"):
                        in_progress_count += 1
                    if (
                        status in ("open", "in_progress", "in-progress", "active")
                        and (row.get("priority") or "").upper() == "P0"
                    ):
                        p0_pending += 1
        except OSError as exc:
            logger.warning("corp_dashboard: cannot read %s: %s", jsonl_path, exc)

    return {
        "open_count": open_count,
        "in_progress_count": in_progress_count,
        "p0_pending": p0_pending,
        "total_scanned": total_scanned,
    }


def _collect_agent_queue() -> Dict[str, Any]:
    """Audit-only view of the agent task queue at ~/.delimit/agents/tasks.json.

    Returns counts for currently-dispatched tasks and tasks that
    transitioned to done/cancelled in the last 24h. NOT a processing
    claim — the dashboard only reads, never writes.
    """
    tasks_path = _delimit_home() / "agents" / "tasks.json"
    if not tasks_path.exists():
        return {
            "dispatched_count": 0,
            "done_today_count": 0,
            "cancelled_today_count": 0,
        }

    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            tasks = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"tasks.json unreadable: {exc}"}

    if not isinstance(tasks, dict):
        return {"error": "tasks.json is not a dict"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    dispatched = 0
    done_today = 0
    cancelled_today = 0

    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        status = (task.get("status") or "").lower()
        if status in ("dispatched", "in_progress", "handed_off"):
            dispatched += 1
            continue

        # Look at completed_at / updated_at for done/cancelled rollups.
        ts_raw = task.get("completed_at") or task.get("updated_at")
        ts: Optional[datetime] = None
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                ts = None
        if ts is None or ts < cutoff:
            continue
        if status == "done":
            done_today += 1
        elif status == "cancelled":
            cancelled_today += 1

    return {
        "dispatched_count": dispatched,
        "done_today_count": done_today,
        "cancelled_today_count": cancelled_today,
    }


def _collect_session() -> Dict[str, Any]:
    """Latest session file: id + revive timestamp + soul id (if any)."""
    sessions_dir = _delimit_home() / "sessions"
    if not sessions_dir.exists():
        return {"last_revive_ts": None, "soul_id": None}

    candidates = sorted(
        sessions_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"last_revive_ts": None, "soul_id": None}

    latest = candidates[0]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"latest session file unreadable: {exc}"}

    return {
        "last_revive_ts": data.get("timestamp") or data.get("ts"),
        "soul_id": data.get("soul_id") or data.get("id"),
        "session_file": latest.name,
    }


# ── Synthesis ─────────────────────────────────────────────────────────


def _safe(fn, default_key: str) -> Dict[str, Any]:
    """Run a sub-section collector. Convert any exception to {"error": ...}.

    Keeps the rest of the dashboard healthy when one source goes bad.
    """
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - generic catch by design
        logger.warning(
            "corp_dashboard: section %r failed: %s", default_key, exc
        )
        return {"error": f"{type(exc).__name__}: {exc}"}


def _build_summary(sections: Dict[str, Any]) -> str:
    """One-line summary used by the session-start ritual.

    Format: "Corp status: <N> daemons active (self-repair, inbox, social),
    <M> ledger open, <P> approvals waiting, <Q> breaches in 24h."
    """
    # Daemons active — count the three known unit names that report
    # active=True. Fall back to in-process daemon flags if systemd is
    # unavailable on this host.
    systemd = sections.get("systemd") or {}
    if systemd.get("available"):
        daemons_active = int(systemd.get("active_count", 0) or 0)
    else:
        # Walk in-process state objects when systemd isn't available.
        daemons_active = 0
        for sub in ("self_repair", "inbox", "social"):
            section = sections.get(sub) or {}
            if isinstance(section, dict) and section.get("running"):
                daemons_active += 1

    ledger = sections.get("ledger") or {}
    ledger_open = int(ledger.get("open_count", 0) or 0)

    inbox = sections.get("inbox") or {}
    approvals = int(inbox.get("pending_approvals", 0) or 0)

    self_repair = sections.get("self_repair") or {}
    breaches = int(self_repair.get("breaches_emitted", 0) or 0)

    return (
        f"Corp status: {daemons_active} daemons active "
        f"(self-repair, inbox, social), "
        f"{ledger_open} ledger open, "
        f"{approvals} approvals waiting, "
        f"{breaches} breaches in 24h."
    )


# ── Public entrypoint ─────────────────────────────────────────────────


def get_corp_dashboard() -> Dict[str, Any]:
    """Single-call synthesis of corp-status across all daemons + ledger
    + inbox + agent queue.

    Returns one dict suitable for the session-start ritual or any
    "is the corp running?" check. Every sub-section is failure-isolated:
    if one collector raises, that key is replaced with {"error": "..."}
    and the rest of the dashboard is returned intact.

    Shape (top level):
        {
          "now": ISO8601,
          "summary": "Corp status: ...",
          "systemd": {available, delimit-self-repair: {...}, ...},
          "self_repair": {...},
          "inbox": {...},
          "social": {...},
          "ledger": {...},
          "agent_queue": {...},
          "session": {...},
        }
    """
    sections: Dict[str, Any] = {
        "systemd": _safe(_collect_systemd, "systemd"),
        "self_repair": _safe(_collect_self_repair, "self_repair"),
        "inbox": _safe(_collect_inbox, "inbox"),
        "social": _safe(_collect_social, "social"),
        "ledger": _safe(_collect_ledger, "ledger"),
        "agent_queue": _safe(_collect_agent_queue, "agent_queue"),
        "session": _safe(_collect_session, "session"),
    }

    out: Dict[str, Any] = {"now": _now_iso(), **sections}
    try:
        out["summary"] = _build_summary(sections)
    except Exception as exc:  # pragma: no cover
        out["summary"] = f"Corp status: summary unavailable ({exc})"
    return out
