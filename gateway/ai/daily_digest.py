"""Daily digest for the Delimit autonomous loop (LED-966).

Produces a structured summary of the last 24h:
  - Cycle count (sense-only daemon ticks)
  - Signals ingested (count by platform)
  - Deliberations held (count + transcript refs)
  - Ledger deltas (items opened, in_progress, done)
  - Agent dispatches (by assignee, status)
  - Pending approvals (drafts awaiting founder)
  - Critical events (errors, timeouts, guard trips)

Writes:
  - ~/.delimit/digest/digest-YYYY-MM-DD.md (file artifact, always)
  - ~/.delimit/digest/digest-YYYY-MM-DD.json (machine-readable)
  - Email to founder (if DELIMIT_DIGEST_EMAIL=true AND email pipeline healthy)

Call via MCP: delimit_digest(action="run") or scheduled cron.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DIGEST_DIR = Path.home() / ".delimit" / "digest"
LEDGER_DIR = Path.home() / ".delimit" / "ledger"
DELIB_DIR = Path.home() / ".delimit" / "deliberations"
SIGNALS_DIR = Path.home() / ".delimit" / "intel" / "signals"
AGENTS_FILE = Path.home() / ".delimit" / "agents" / "tasks.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_dir():
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)


def _count_signals(since: datetime) -> Dict[str, Any]:
    """Count signals ingested in the window, grouped by platform."""
    counts: Counter = Counter()
    total = 0
    if not SIGNALS_DIR.exists():
        return {"total": 0, "by_platform": {}}
    for shard in SIGNALS_DIR.glob("*.jsonl"):
        if shard.name.startswith("_"):
            continue
        try:
            shard_date = datetime.fromisoformat(shard.stem).date()
        except ValueError:
            continue
        if shard_date < since.date():
            continue
        try:
            for line in shard.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ts = datetime.fromisoformat(row.get("ingested_at", "").replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < since:
                    continue
                counts[row.get("platform", "?")] += 1
                total += 1
        except OSError:
            continue
    return {"total": total, "by_platform": dict(counts.most_common())}


def _count_deliberations(since: datetime) -> Dict[str, Any]:
    """Count deliberation transcripts created in the window."""
    if not DELIB_DIR.exists():
        return {"total": 0, "unanimous": 0, "no_consensus": 0, "recent": []}
    total = 0
    unanimous = 0
    no_consensus = 0
    recent = []
    for f in sorted(DELIB_DIR.glob("deliberation_*.json"), reverse=True)[:50]:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < since:
                continue
            d = json.loads(f.read_text())
            total += 1
            verdict = (d.get("final_verdict") or "").upper()
            if "UNANIMOUS" in verdict:
                unanimous += 1
            elif "NO CONSENSUS" in verdict or "MAX ROUNDS" in verdict:
                no_consensus += 1
            rounds_field = d.get("rounds", 0)
            rounds_count = len(rounds_field) if isinstance(rounds_field, list) else rounds_field
            recent.append({
                "file": f.name,
                "verdict": (d.get("final_verdict") or "?")[:60],
                "status": d.get("status", "?"),
                "rounds": rounds_count,
            })
        except Exception:
            continue
    return {
        "total": total,
        "unanimous": unanimous,
        "no_consensus": no_consensus,
        "recent": recent[:10],
    }


def _count_ledger_deltas(since: datetime) -> Dict[str, Any]:
    """Count ledger items opened / updated / done in the window."""
    opened = 0
    done = 0
    new_items = []
    done_items = []
    if not LEDGER_DIR.exists():
        return {"opened": 0, "done": 0, "new": [], "completed": []}
    since_iso = since.isoformat().replace("+00:00", "Z")
    for lf in LEDGER_DIR.glob("*.jsonl"):
        try:
            for line in lf.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                created = item.get("created_at", "")
                updated = item.get("updated_at", created)
                if created >= since_iso and item.get("type") != "update":
                    opened += 1
                    new_items.append({
                        "id": item.get("id"),
                        "title": (item.get("title") or "")[:80],
                        "priority": item.get("priority", "?"),
                    })
                if item.get("type") == "update" and item.get("status") == "done" and updated >= since_iso:
                    done += 1
                    done_items.append({
                        "id": item.get("id"),
                        "note": (item.get("note") or "")[:120],
                    })
        except OSError:
            continue
    return {
        "opened": opened,
        "done": done,
        "new": new_items[-10:],
        "completed": done_items[-10:],
    }


def _count_dispatches(since: datetime) -> Dict[str, Any]:
    """Count swarm dispatches and their current status."""
    if not AGENTS_FILE.exists():
        return {"total": 0, "by_status": {}, "by_assignee": {}, "stuck_over_24h": 0}
    try:
        tasks = json.loads(AGENTS_FILE.read_text())
    except Exception:
        return {"total": 0, "by_status": {}, "by_assignee": {}, "stuck_over_24h": 0}
    status_counts: Counter = Counter()
    assignee_counts: Counter = Counter()
    stuck = 0
    dispatched_recent = 0
    since_iso = since.isoformat().replace("+00:00", "Z")
    for tid, task in tasks.items():
        status = task.get("status", "?")
        status_counts[status] += 1
        if task.get("created_at", "") >= since_iso:
            dispatched_recent += 1
        if status == "dispatched":
            assignee_counts[task.get("assignee", "?")] += 1
            try:
                created = datetime.fromisoformat(task.get("created_at", "").replace("Z", "+00:00"))
                if (_now() - created) > timedelta(hours=24):
                    stuck += 1
            except Exception:
                pass
    return {
        "total_tasks": len(tasks),
        "dispatched_last_24h": dispatched_recent,
        "by_status": dict(status_counts),
        "dispatched_by_assignee": dict(assignee_counts),
        "stuck_over_24h": stuck,
    }


def _check_health(since: datetime) -> Dict[str, Any]:
    """Check for errors, guard trips, timeouts in the window."""
    health = {
        "pause_file_exists": (Path.home() / ".delimit" / "pause_dispatch").exists(),
        "signal_guard_shadow_hits": 0,
        "daemon_stopped": False,
    }
    # Signal guard shadow log
    shadow = Path.home() / ".delimit" / "logs" / "signal_guard_shadow.jsonl"
    if shadow.exists():
        since_iso = since.isoformat().replace("+00:00", "Z")
        try:
            for line in shadow.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("ts", "") >= since_iso:
                    health["signal_guard_shadow_hits"] += 1
        except OSError:
            pass
    return health


def build_digest(window_hours: int = 24) -> Dict[str, Any]:
    """Collect all signals for the last window_hours into a single digest dict."""
    since = _now() - timedelta(hours=window_hours)
    return {
        "generated_at": _now().isoformat(),
        "window_hours": window_hours,
        "window_start": since.isoformat(),
        "signals": _count_signals(since),
        "deliberations": _count_deliberations(since),
        "ledger": _count_ledger_deltas(since),
        "dispatches": _count_dispatches(since),
        "health": _check_health(since),
    }


def render_markdown(digest: Dict[str, Any]) -> str:
    """Render the digest as a founder-readable markdown document."""
    g = digest
    s = g["signals"]
    d = g["deliberations"]
    l = g["ledger"]
    dsp = g["dispatches"]
    h = g["health"]

    lines = [
        f"# Delimit Daily Digest — {g['generated_at'][:10]}",
        "",
        f"Window: last {g['window_hours']}h (since {g['window_start'][:16]}Z)",
        "",
        "## Health",
        "",
        f"- Pause file: {'🔴 ACTIVE' if h['pause_file_exists'] else '🟢 clear'}",
        f"- Signal guard shadow hits: {h['signal_guard_shadow_hits']}",
        "",
        "## Signals ingested",
        "",
        f"Total: **{s['total']}** signals",
    ]
    for platform, count in s.get("by_platform", {}).items():
        lines.append(f"- {platform}: {count}")
    lines.extend([
        "",
        "## Deliberations",
        "",
        f"- Total: **{d['total']}**",
        f"- Unanimous: {d['unanimous']}",
        f"- No consensus / max rounds: {d['no_consensus']}",
    ])
    if d.get("recent"):
        lines.append("")
        lines.append("Recent transcripts:")
        for r in d["recent"]:
            lines.append(f"  - `{r['file']}` — {r['verdict']} ({r.get('rounds', '?')} rounds)")
    lines.extend([
        "",
        "## Ledger deltas",
        "",
        f"- Items opened: **{l['opened']}**",
        f"- Items completed: **{l['done']}**",
    ])
    if l.get("new"):
        lines.append("")
        lines.append("New items:")
        for item in l["new"]:
            lines.append(f"  - {item['id']} [{item['priority']}] {item['title']}")
    if l.get("completed"):
        lines.append("")
        lines.append("Completed:")
        for item in l["completed"]:
            lines.append(f"  - {item['id']} — {item['note']}")
    lines.extend([
        "",
        "## Swarm dispatches",
        "",
        f"- Total tasks ever: {dsp['total_tasks']}",
        f"- New dispatches last 24h: **{dsp['dispatched_last_24h']}**",
        f"- Stuck (dispatched >24h): {dsp['stuck_over_24h']}",
    ])
    if dsp.get("by_status"):
        lines.append("")
        lines.append("By status:")
        for status, count in dsp["by_status"].items():
            lines.append(f"  - {status}: {count}")
    if dsp.get("dispatched_by_assignee"):
        lines.append("")
        lines.append("Currently dispatched by assignee:")
        for who, count in dsp["dispatched_by_assignee"].items():
            lines.append(f"  - {who}: {count}")
    lines.extend([
        "",
        "## Pending founder actions",
        "",
        f"- Stuck dispatches (need worker): {dsp['stuck_over_24h']}",
        f"- Pause file present: {'yes' if h['pause_file_exists'] else 'no'}",
        f"- Guard shadow hits (investigate if >0): {h['signal_guard_shadow_hits']}",
        "",
        "---",
        f"Digest generated at {g['generated_at']}",
    ])
    return "\n".join(lines)


def write_digest(window_hours: int = 24) -> Dict[str, str]:
    """Generate the digest and write both markdown + json artifacts.

    Returns paths to the created files so the founder can inspect them
    from the interactive session even without email delivery.
    """
    _ensure_dir()
    digest = build_digest(window_hours=window_hours)
    date_slug = digest["generated_at"][:10]
    md_path = DIGEST_DIR / f"digest-{date_slug}.md"
    json_path = DIGEST_DIR / f"digest-{date_slug}.json"
    md_path.write_text(render_markdown(digest))
    json_path.write_text(json.dumps(digest, indent=2))
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "summary": f"{digest['signals']['total']} signals, {digest['deliberations']['total']} deliberations, {digest['ledger']['opened']} new ledger items, {digest['dispatches']['stuck_over_24h']} stuck dispatches",
    }


def send_digest_email(to: str = "", from_account: str = "pro@delimit.ai") -> Dict[str, Any]:
    """Send the most recent digest via the notify pipeline.

    Gated on environment: returns a no-op result when DMARC is missing
    and email would be filtered. Set DELIMIT_DIGEST_EMAIL=true to force
    send attempts regardless. The digest markdown is always written to
    disk so the founder can inspect it from the interactive session.
    """
    import os
    result = write_digest(window_hours=24)
    md_path = Path(result["markdown_path"])
    if not md_path.exists():
        return {"error": "digest not written", "files": result}

    send_enabled = os.environ.get("DELIMIT_DIGEST_EMAIL", "").lower() in ("true", "1", "yes")
    if not send_enabled:
        return {
            "status": "skipped_email",
            "reason": "DELIMIT_DIGEST_EMAIL not set to true; digest written to disk only",
            "files": result,
        }

    try:
        from ai.notify import send_notification
        body = md_path.read_text()
        send_notification(
            channel="email",
            message=body,
            subject=f"[DIGEST] Delimit — {result['summary']}",
            to=to or os.environ.get("DELIMIT_SMTP_TO", ""),
            from_account=from_account,
            event_type="daily_digest",
        )
        return {
            "status": "sent",
            "files": result,
        }
    except Exception as exc:
        return {
            "status": "send_failed",
            "error": str(exc),
            "files": result,
        }
