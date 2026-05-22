"""
Self-repair knowledge-base append/read layer.

Writes one canonical line per (breach, deliberation, verdict) tuple to
`~/.delimit/self_repair_history.jsonl`. This file is the data substrate
for the eventual rule-first triage (post-graduation): once N≥5
deliberations exist for a function, the apply-mode layer can extract
recurring patterns from this file and short-circuit deliberation for
known-good fix tiers.

The schema is intentionally narrow — one line per verdict, each line a
flat JSON object. `founder_decision` starts as null and is updated by
the next-layer apply gate when the founder's email reply lands.

Public API:
    append_history(verdict, *, history_path=None) -> Path
    iter_history(*, history_path=None) -> Iterable[dict]
    count_in_window(function, *, since=None, until=None,
                    history_path=None) -> int
    update_decision(action_id, decision, *, history_path=None) -> bool

Constraints honored:
  - Pure stdlib (json + pathlib + datetime).
  - Append-only — never truncate or rewrite the file. Updates to
    `founder_decision` rewrite the file in-memory then atomically
    replace it (preserves order; never drops history).
  - No retries on disk failure — the watcher must not stall on a
    transient FS error; callers log + continue.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .deliberate import DeliberationVerdict, verdict_to_dict

logger = logging.getLogger("delimit.ai.self_repair.history")

DEFAULT_HISTORY_PATH = Path.home() / ".delimit" / "self_repair_history.jsonl"


# ── canonical schema ────────────────────────────────────────────────


def _build_record(verdict: DeliberationVerdict) -> Dict[str, Any]:
    """Build the canonical JSONL record for a verdict.

    Schema (locked for v1 — additions OK, renames/removals not):
        ts                  ISO-8601 UTC of verdict capture
        function            corp function name (e.g. marketing.social_outreach)
        breach_kpi          KPI name that breached
        severity            warning | critical
        actual              float — measured KPI value
        threshold           float — floor or ceiling that was crossed
        fix_tier            one of FIX_TIERS (deliberate.py)
        fix_description     short human string
        escalation_class    list[str] — empty unless hard-stops tripped
        verdict_status      unanimous | split | error
        transcript_path     where the markdown transcript lives
        requires_founder_approval  bool
        founder_decision    null until apply-layer updates (approve/reject/info)
        action_id           identifier surfaced in the email
    """
    breach = verdict.breach
    # Recompute the action_id the email exposes so the apply gate can
    # correlate. Mirrors `render_verdict_email`.
    from .deliberate import _safe_segment as _seg

    action_id = (
        f"sr-{_seg(breach.function)}-"
        f"{_seg(breach.kpi_name)}-"
        f"{_seg(verdict.timestamp.replace(':', ''))}"
    )

    fix = verdict.proposed_fix or {}
    return {
        "ts": verdict.timestamp,
        "function": breach.function,
        "breach_kpi": breach.kpi_name,
        "severity": breach.severity,
        "actual": breach.actual,
        "threshold": breach.threshold,
        "fix_tier": fix.get("tier"),
        "fix_description": fix.get("description"),
        "escalation_class": list(verdict.escalation_class),
        "verdict_status": verdict.status,
        "transcript_path": verdict.transcript_path,
        "requires_founder_approval": verdict.requires_founder_approval,
        "founder_decision": None,
        "action_id": action_id,
    }


# ── append ─────────────────────────────────────────────────────────


def append_history(
    verdict: DeliberationVerdict,
    *,
    history_path: Optional[Path] = None,
) -> Path:
    """Append one verdict record to `history_path`.

    Returns the path that was written. Callers that need the record
    body for further processing should construct it themselves via
    `_build_record` — we only return the path so the watcher can log
    where evidence landed.

    Errors during write are logged and re-raised — the watcher catches
    them and continues. We do NOT swallow IO errors silently because
    history loss is a moat-component failure (per the V2-hardened
    "what must never be lost" list in CLAUDE.md).
    """
    target = history_path or DEFAULT_HISTORY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    record = _build_record(verdict)
    line = json.dumps(record, default=str, sort_keys=True)

    with open(target, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return target


# ── read / aggregate ───────────────────────────────────────────────


def iter_history(
    *, history_path: Optional[Path] = None
) -> Iterator[Dict[str, Any]]:
    """Yield records from the history file, oldest first.

    Skips bad lines with a debug log. Returns nothing if the file is
    missing — callers should treat absent history as empty, not as an
    error.
    """
    target = history_path or DEFAULT_HISTORY_PATH
    if not target.exists():
        return
    with open(target, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "self_repair: skipping bad history line %s:%d — %s",
                    target,
                    lineno,
                    exc,
                )
                continue
            if isinstance(obj, dict):
                yield obj


def count_in_window(
    function: str,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    history_path: Optional[Path] = None,
    kpi: Optional[str] = None,
) -> int:
    """Count history records for `function` inside [since, until].

    Used by the watcher to enforce `max_deliberations_per_week` (and,
    per LED-1210, `max_deliberations_per_kpi_per_week`). Both bounds
    are inclusive; if `since`/`until` is None, that side is unbounded.

    `kpi` (LED-1210): when set, also constrain the count to records
    where `breach_kpi == kpi`. Default None preserves the prior
    function-wide behavior so existing callers aren't broken.
    """
    n = 0
    for record in iter_history(history_path=history_path):
        if record.get("function") != function:
            continue
        if kpi is not None and record.get("breach_kpi") != kpi:
            continue
        ts_raw = record.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        n += 1
    return n


def deliberations_this_week(
    function: str,
    *,
    now: Optional[datetime] = None,
    history_path: Optional[Path] = None,
    kpi: Optional[str] = None,
) -> int:
    """Convenience: how many deliberations for `function` in the last 7d.

    `kpi` (LED-1210): when set, narrows to a specific (function, kpi)
    pair. Default None counts every deliberation for the function —
    same behavior as before, so existing callers (and the function-
    level `max_deliberations_per_week` cap) are unaffected.
    """
    anchor = now or datetime.now(tz=timezone.utc)
    since = anchor - timedelta(days=7)
    return count_in_window(
        function,
        since=since,
        until=anchor,
        history_path=history_path,
        kpi=kpi,
    )


# ── update (used by next-layer apply gate) ─────────────────────────


def update_decision(
    action_id: str,
    decision: str,
    *,
    history_path: Optional[Path] = None,
) -> bool:
    """Mark a record's `founder_decision` field.

    Atomic: rewrites the file via a tmp-and-rename so a crash mid-write
    cannot truncate history. Returns True iff a record matched
    `action_id` and was updated.

    Valid `decision` values are not enforced here — the caller (apply
    layer) is the one that knows the vocabulary. Typical values:
    "approve", "reject", "info", "applied", "verified", "failed".
    """
    target = history_path or DEFAULT_HISTORY_PATH
    if not target.exists():
        return False

    records: List[Dict[str, Any]] = list(iter_history(history_path=target))
    matched = False
    for rec in records:
        if rec.get("action_id") == action_id:
            rec["founder_decision"] = decision
            matched = True

    if not matched:
        return False

    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
    tmp.replace(target)
    return True
