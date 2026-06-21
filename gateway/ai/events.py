"""Event ingestion for dashboard real-time feed."""
import json
import time
from pathlib import Path
from datetime import datetime

EVENTS_DIR = Path.home() / ".delimit" / "events"


def emit(event_type: str, tool: str, model: str = "", detail: str = "", venture: str = ""):
    """Write an event to the daily events log."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    event = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "type": event_type,  # tool_call, governance_check, deliberation, deploy, error
        "tool": tool,
        "model": model,
        "detail": detail,
        "venture": venture,
    }
    with open(EVENTS_DIR / f"events-{today}.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")


def recent(limit: int = 50) -> list:
    """Get the most recent events across all days."""
    events = []
    if not EVENTS_DIR.exists():
        return events
    for f in sorted(EVENTS_DIR.glob("events-*.jsonl"), reverse=True):
        for line in reversed(f.read_text().splitlines()):
            try:
                events.append(json.loads(line))
            except Exception:
                pass
            if len(events) >= limit:
                return events
    return events


def pro_gate_denial_summary(days: int = 30) -> dict:
    """Free->Pro funnel signal (LED-1755): count `pro_gate_denied` events by
    tool over the last `days` days. A high count = strong upgrade INTENT for
    that tool — i.e. where the upgrade CTA has the most leverage. Read-only."""
    from collections import Counter

    counts: Counter = Counter()
    total = 0
    if not EVENTS_DIR.exists():
        return {"days": days, "total_denials": 0, "by_tool": {}}
    cutoff = time.time() - days * 86400
    for f in sorted(EVENTS_DIR.glob("events-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") != "pro_gate_denied":
                continue
            try:
                ev_t = datetime.fromisoformat(ev.get("ts", "").replace("Z", "")).timestamp()
            except Exception:
                ev_t = cutoff  # undated → include rather than silently drop
            if ev_t < cutoff:
                continue
            counts[ev.get("tool", "?")] += 1
            total += 1
    return {"days": days, "total_denials": total, "by_tool": dict(counts.most_common())}
