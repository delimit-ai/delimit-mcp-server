"""Persistent scan queue for the social distribution pipeline (LED-216 Phase 2).

The queue decouples target *scanning* from draft *generation*. Scanners append
discovered targets here; the draft phase claims pending entries, generates a
post, and marks them drafted (or failed). This allows the Reddit scanner to
take 120s without blocking X/HN/devto/etc., and lets the draft stage run
under its own timeout against a stable backlog.

Storage: append-only JSONL at ``~/.delimit/social_scan_queue.jsonl``. Each
line is a single queue entry with the schema:

    {
        "queued_at": "2026-05-02T15:30:00Z",
        "platform": "reddit",
        "fingerprint": "reddit:vibecoding:abc123",
        "source_id": "...",
        "raw": { /* full target dict from the scanner */ },
        "status": "pending",       # pending | drafted | drafted_failed | expired
        "drafted_at": null,
        "draft_id": null,
        "error": null
    }

All writes are atomic (tmp + rename for full rewrites; append-only for the
hot path). Malformed lines are skipped with a warning rather than crashing
the whole queue. Dedupe-on-enqueue avoids re-queuing a fingerprint that is
already pending or was drafted within the last 7 days.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUEUE_FILE = Path.home() / ".delimit" / "social_scan_queue.jsonl"

DEFAULT_DEDUPE_HOURS = 24 * 7  # don't re-queue a fingerprint within 7 days
DEFAULT_EXPIRE_HOURS = 24 * 7  # entries older than 7 days roll to expired

# Per-platform freshness cap at claim_pending time. Reddit posts decay
# in comment-visibility VERY fast (Boris-Cherny LED-1335: <6h high-yield,
# <12h marginal, ~zero after 24h), so drafting on a 3-day-old post wastes
# a brand-account engagement. Other platforms (github, devto) have longer
# half-lives — issue threads can be relevant weeks later — so we don't
# apply the freshness cap there.
#
# Founder regression report 2026-05-18: drafts were being generated on
# posts queued 3+ days earlier because the queue is FIFO and the drafter
# falls behind the scanner. This filter ensures claim_pending() never
# returns reddit entries whose `queued_at` is more than CLAIM_FRESHNESS_HOURS
# old, regardless of queue position.
CLAIM_FRESHNESS_HOURS_BY_PLATFORM: Dict[str, int] = {
    "reddit": 24,
    # Phase C (2026-05-18): github targets fail ~96% of the time
    # (historical 3256/3389 marked drafted_failed) and the queue grew
    # to 1122 pending dominating FIFO order. 24h cap drains stale crud
    # while preserving fresh github targets the drafter would actually
    # process. Without this, github starves reddit/x/hn even with the
    # round-robin claim (see CLAIM_MAX_PER_PLATFORM).
    "github": 24,
}

# Phase C (2026-05-18): round-robin claim_pending across platforms so a
# noisy platform doesn't starve quieter ones. Drafter calls claim_pending
# with limit=10; pre-Phase-C this returned 10 oldest entries regardless
# of platform, which with github=1122 pending meant 10/10 github and
# reddit drafts never fired. With this cap, drafter sees a balanced mix.
# Within-platform order is FIFO (oldest pending first) EXCEPT for
# platforms in CLAIM_LIFO_PLATFORMS — see below.
CLAIM_MAX_PER_PLATFORM: int = 3

# Phase D (2026-05-18 founder request): "we need first-poster advantage."
# For time-critical engagement platforms, the drafter should pick the
# FRESHEST pending entry, not the oldest. Reddit comment visibility decays
# sharply after the first 15-30 minutes of a thread (the first 5-10 visible
# comments capture the bulk of upvotes + clickthrough). Pre-Phase-D's
# within-reddit FIFO meant the drafter pulled 22-24h-old entries (near
# the freshness cap) instead of brand-new ones.
#
# LIFO-within-platform reverses that for the listed platforms: within the
# eligible bucket, sort newest queued_at first. Across platforms, round-
# robin still applies. Entries that get displaced by newer ones are
# naturally cleaned up by the freshness cap groomer.
#
# Other platforms (github, devto, etc.) keep FIFO — their content has a
# longer half-life and oldest-first is the right discipline there.
CLAIM_LIFO_PLATFORMS: set = {"reddit"}

PENDING = "pending"
DRAFTED = "drafted"
DRAFTED_FAILED = "drafted_failed"
EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _queue_path() -> Path:
    """Resolve the queue file path. Indirection lets tests monkeypatch it."""
    return QUEUE_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # ``fromisoformat`` accepts both naive and tz-aware strings.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _iter_entries(path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
    """Yield each parsable JSON line from the queue. Skip and log malformed."""
    p = path or _queue_path()
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for ln_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "social_queue: skipping malformed line %d in %s: %s",
                        ln_no, p, exc,
                    )
                    continue
    except OSError as exc:
        logger.warning("social_queue: failed to read %s: %s", p, exc)


def _atomic_rewrite(entries: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    """Rewrite the queue file atomically via tmp + os.replace."""
    p = path or _queue_path()
    _ensure_parent(p)
    fd, tmp_path = tempfile.mkstemp(prefix=".social_scan_queue.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_atomic(entry: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append a single line. fsync-friendly: opens in 'a', writes, closes."""
    p = path or _queue_path()
    _ensure_parent(p)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(target: Dict[str, Any], dedupe_hours: int = DEFAULT_DEDUPE_HOURS) -> Optional[str]:
    """Append a scanner target to the queue.

    Returns the fingerprint string on success, or ``None`` when the target
    was deduped (already pending, or drafted within the dedupe window).
    Targets without a ``fingerprint`` field are rejected.
    """
    fingerprint = (target or {}).get("fingerprint")
    if not fingerprint:
        logger.warning("social_queue.enqueue: target missing fingerprint, skipping")
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=dedupe_hours)

    # Dedupe: scan existing entries for a recent matching fingerprint.
    for existing in _iter_entries():
        if existing.get("fingerprint") != fingerprint:
            continue
        status = existing.get("status")
        if status == PENDING:
            return None
        if status == DRAFTED:
            ts = _parse_iso(existing.get("drafted_at") or existing.get("queued_at"))
            if ts and ts >= cutoff:
                return None
        # drafted_failed or expired or older drafted → allow re-enqueue

    entry = {
        "queued_at": _now_iso(),
        "platform": target.get("platform", ""),
        "fingerprint": fingerprint,
        "source_id": target.get("source_id", ""),
        "raw": target,
        "status": PENDING,
        "drafted_at": None,
        "draft_id": None,
        "error": None,
    }
    _append_atomic(entry)
    return fingerprint


def claim_pending(platform: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Return up to ``limit`` pending entries, with round-robin balancing
    across platforms when ``platform`` is None.

    Read-only — does NOT mutate state. The caller must call ``mark_drafted``
    or ``mark_failed`` once it processes the entry.

    Round-robin (Phase C, 2026-05-18): without a platform filter, returns
    at most CLAIM_MAX_PER_PLATFORM entries from any single platform per
    call. Within each platform, oldest-first (FIFO). Across platforms,
    interleaved so the drafter sees a balanced mix instead of saturating
    on whichever platform has the deepest backlog.

    With an explicit ``platform`` filter, behaves as FIFO over that single
    platform's pending entries (no per-platform cap, since the caller is
    already targeting one platform).

    Freshness cap (Phase A, 2026-05-18): entries whose platform has a
    CLAIM_FRESHNESS_HOURS_BY_PLATFORM cap and whose ``queued_at`` is
    older than that cap are skipped silently. Those stale entries stay
    in the file with status=pending; the separate ``expire_stale_for_
    freshness_caps`` pass flips them so they don't pile up forever.
    """
    now = datetime.now(timezone.utc)

    def _is_eligible(entry: Dict[str, Any]) -> bool:
        if entry.get("status") != PENDING:
            return False
        if platform and entry.get("platform") != platform:
            return False
        cap = CLAIM_FRESHNESS_HOURS_BY_PLATFORM.get(entry.get("platform"))
        if cap is not None:
            qts = _parse_iso(entry.get("queued_at"))
            if qts is not None and (now - qts) > timedelta(hours=cap):
                return False
        return True

    # Single-platform filter path: keep legacy strict-FIFO behavior.
    if platform:
        out: List[Dict[str, Any]] = []
        for entry in _iter_entries():
            if not _is_eligible(entry):
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    # Round-robin path: group by platform first (preserving within-platform
    # FIFO via iteration order), then cap per-platform and interleave.
    # CRITICAL Phase D change: for CLAIM_LIFO_PLATFORMS, collect the full
    # eligible set per platform first, then sort newest-first BEFORE
    # truncation — otherwise the early-break truncation in the FIFO path
    # would keep the oldest entries even when we want the newest.
    by_platform: Dict[str, List[Dict[str, Any]]] = {}
    for entry in _iter_entries():
        if not _is_eligible(entry):
            continue
        plat = entry.get("platform") or "unknown"
        by_platform.setdefault(plat, []).append(entry)

    # Sort + truncate each bucket.
    for plat in list(by_platform.keys()):
        if plat in CLAIM_LIFO_PLATFORMS:
            # Newest queued_at first. Parse-failures sort last so a
            # corrupted-timestamp entry doesn't block legitimate fresh
            # entries from being claimed.
            by_platform[plat].sort(
                key=lambda e: _parse_iso(e.get("queued_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
        # FIFO platforms keep insertion order (which is JSONL-append =
        # oldest first); no sort needed.
        by_platform[plat] = by_platform[plat][:CLAIM_MAX_PER_PLATFORM]

    # Interleave: round-robin across platforms in alphabetical order for
    # determinism. Stop when limit is reached or all buckets are drained.
    out2: List[Dict[str, Any]] = []
    plat_order = sorted(by_platform.keys())
    idx = {p: 0 for p in plat_order}
    while len(out2) < limit:
        added = False
        for p in plat_order:
            if idx[p] < len(by_platform[p]):
                out2.append(by_platform[p][idx[p]])
                idx[p] += 1
                added = True
                if len(out2) >= limit:
                    break
        if not added:
            break
    return out2


def expire_stale_for_freshness_caps() -> Dict[str, int]:
    """Roll pending entries past their platform's claim freshness cap to expired.

    Companion to ``claim_pending``'s in-flight skip: without this, the
    queue file fills up with pending-but-permanently-skipped entries
    that we still re-scan on every claim. Returns a dict
    ``{platform: count_expired}`` for observability.
    """
    now = datetime.now(timezone.utc)
    entries = list(_iter_entries())
    if not entries:
        return {}
    flipped: Dict[str, int] = {}
    changed = False
    for entry in entries:
        if entry.get("status") != PENDING:
            continue
        plat = entry.get("platform")
        cap = CLAIM_FRESHNESS_HOURS_BY_PLATFORM.get(plat)
        if cap is None:
            continue
        qts = _parse_iso(entry.get("queued_at"))
        if qts is None or (now - qts) <= timedelta(hours=cap):
            continue
        entry["status"] = EXPIRED
        entry["error"] = f"expired_freshness_cap_{cap}h"
        flipped[plat] = flipped.get(plat, 0) + 1
        changed = True
    if changed:
        _atomic_rewrite(entries)
    return flipped


def _update_entry(fingerprint: str, mutator) -> bool:
    """Apply ``mutator(entry) -> entry`` to the most recent entry matching
    fingerprint with status==pending. Returns True on hit, False otherwise.
    Rewrites the queue atomically.
    """
    entries = list(_iter_entries())
    target_idx = -1
    for idx in range(len(entries) - 1, -1, -1):
        e = entries[idx]
        if e.get("fingerprint") == fingerprint and e.get("status") == PENDING:
            target_idx = idx
            break
    if target_idx < 0:
        return False
    entries[target_idx] = mutator(entries[target_idx])
    _atomic_rewrite(entries)
    return True


def mark_drafted(fingerprint: str, draft_id: str) -> bool:
    """Mark a pending entry as drafted. Returns True on hit."""
    def _mutate(entry: Dict[str, Any]) -> Dict[str, Any]:
        entry["status"] = DRAFTED
        entry["drafted_at"] = _now_iso()
        entry["draft_id"] = draft_id or ""
        entry["error"] = None
        return entry
    return _update_entry(fingerprint, _mutate)


def mark_failed(fingerprint: str, error: str) -> bool:
    """Mark a pending entry as drafted_failed with the supplied error string."""
    def _mutate(entry: Dict[str, Any]) -> Dict[str, Any]:
        entry["status"] = DRAFTED_FAILED
        entry["drafted_at"] = _now_iso()
        entry["error"] = (error or "")[:500]
        return entry
    return _update_entry(fingerprint, _mutate)


def expire_older_than(hours: int = DEFAULT_EXPIRE_HOURS) -> int:
    """Roll any pending entry older than ``hours`` into the expired status.

    Returns the count expired. Entries past the window are still kept in the
    file (for audit), but their status flips so they no longer surface to
    ``claim_pending``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries = list(_iter_entries())
    if not entries:
        return 0
    flipped = 0
    for entry in entries:
        if entry.get("status") != PENDING:
            continue
        ts = _parse_iso(entry.get("queued_at"))
        if ts is None or ts >= cutoff:
            continue
        entry["status"] = EXPIRED
        entry["error"] = "expired_after_window"
        flipped += 1
    if flipped:
        _atomic_rewrite(entries)
    return flipped


def queue_stats() -> Dict[str, Any]:
    """Return a snapshot count: ``{pending, drafted, drafted_failed, expired,
    by_platform: {platform: {pending, drafted, ...}}}``.
    """
    totals = {PENDING: 0, DRAFTED: 0, DRAFTED_FAILED: 0, EXPIRED: 0}
    by_platform: Dict[str, Dict[str, int]] = {}
    for entry in _iter_entries():
        status = entry.get("status") or "unknown"
        if status in totals:
            totals[status] += 1
        plat = entry.get("platform") or "unknown"
        plat_bucket = by_platform.setdefault(plat, {PENDING: 0, DRAFTED: 0, DRAFTED_FAILED: 0, EXPIRED: 0})
        if status in plat_bucket:
            plat_bucket[status] += 1
    return {
        "pending": totals[PENDING],
        "drafted": totals[DRAFTED],
        "drafted_failed": totals[DRAFTED_FAILED],
        "expired": totals[EXPIRED],
        "by_platform": by_platform,
    }


__all__ = [
    "QUEUE_FILE",
    "PENDING",
    "DRAFTED",
    "DRAFTED_FAILED",
    "EXPIRED",
    "enqueue",
    "claim_pending",
    "mark_drafted",
    "mark_failed",
    "expire_older_than",
    "queue_stats",
]
