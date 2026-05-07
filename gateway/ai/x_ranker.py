"""X engagement ranker (LED-216 Phase 2, Q4 of the 2026-05-02 distribution panel).

Filters and orders X (Twitter) candidate posts so we reply to the highest-
yield originals only. The score formula and the 7-day author dedupe are the
panel's Q4 conclusion: Codex's age-normalized formula plus Opus's API filters
(``-is:retweet -is:reply lang:en``) and the founder's anti-spammy posture
("don't hammer the same 5 accounts daily").

Score formula (per the LED-216 Phase 2 directive):

    score = (likes + 2 * retweets + 0.5 * replies + 3 * quotes) / max(age_hours, 1)

Filter pipeline applied in order:
    1. ``is_op`` → drop reply-chain targets (we only reply to OPs)
    2. ``lang == 'en'`` → drop non-English (US/UK builder community is the wedge)
    3. ``is_retweet == False`` → drop retweets
    4. dedupe authors we replied to in the last ``replied_authors_window_hours``
    5. sort by score DESC

Tolerant defaults: any field missing on a target is treated as "do not drop"
so partial Twttr241 payloads still rank instead of being silently filtered.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


SOCIAL_LOG = Path.home() / ".delimit" / "social_log.jsonl"

DEFAULT_WINDOW_HOURS = 24 * 7  # 7 days, per founder's anti-spammy directive


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _social_log_path() -> Path:
    """Indirection so tests can monkeypatch the log location."""
    return SOCIAL_LOG


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_twitter_created_at(value: Optional[str]) -> Optional[datetime]:
    """Twttr241 ``created_at`` is a Twitter-style timestamp like
    ``Wed Apr 30 14:23:55 +0000 2026``. Falls back to ISO 8601 for caches
    that have already normalized the value.
    """
    if not value:
        return None
    iso = _parse_iso(value)
    if iso is not None:
        return iso
    try:
        dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None
    return dt


def _normalize_handle(handle: Optional[str]) -> str:
    if not handle:
        return ""
    h = handle.strip().lower()
    if h.startswith("@"):
        h = h[1:]
    return h


def _replied_authors_within(window_hours: int, log_path: Optional[Path] = None) -> Set[str]:
    """Read ``social_log.jsonl`` and return the set of author handles we
    replied to on Twitter inside the window. Tolerant of malformed lines.

    Twitter replies log either ``replying_to_user`` (Reddit field, rare on X)
    or carry the original author inside the draft text — we cannot recover
    that retroactively. The reliable signal is ``handle`` (us) plus
    ``reply_to_id`` (their tweet id). Without the id->author mapping we use
    a best-effort: a stored ``replying_to_user`` field if present, else any
    ``@handle`` token at the start of the post text. Both are conservative
    forms of dedupe — better to skip a borderline candidate than spam.
    """
    p = log_path or _social_log_path()
    if not p.exists():
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    authors: Set[str] = set()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("platform") != "twitter":
                    continue
                ts = _parse_iso(entry.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                # Preferred: explicit replying_to_user field
                explicit = _normalize_handle(entry.get("replying_to_user"))
                if explicit:
                    authors.add(explicit)
                    continue
                # Fallback: leading @handle in the post text. This is the way
                # X reply text starts when the client appends the reply prefix.
                text = (entry.get("text") or "").lstrip()
                if text.startswith("@"):
                    # nosec — strips leading @ from Twitter handle, not a credential
                    token = text.split()[0][1:]
                    token = "".join(c for c in token if c.isalnum() or c == "_").lower()
                    if token:
                        authors.add(token)
    except OSError as exc:
        logger.warning("x_ranker: failed to read %s: %s", p, exc)
    return authors


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _age_hours(target: Dict[str, Any]) -> float:
    """Best-effort age in hours from ``target['created_at']`` (ISO or Twitter
    style). Missing / unparseable timestamps return ``1.0`` so the score is
    not divided by something pathological — and the candidate is still
    scoreable rather than dropped.
    """
    raw = target.get("created_at") or target.get("created") or ""
    dt = _parse_twitter_created_at(raw) or _parse_iso(raw)
    if dt is None:
        return 1.0
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600.0
    if hours < 1.0:
        return 1.0
    return hours


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def score_target(target: Dict[str, Any]) -> float:
    """Engagement-rate score for a single X candidate.

    score = (likes + 2*retweets + 0.5*replies + 3*quotes) / max(age_hours, 1)
    """
    likes = _coerce_int(target.get("likes") or target.get("favorite_count"))
    retweets = _coerce_int(target.get("retweets") or target.get("retweet_count"))
    replies = _coerce_int(target.get("reply_count") or target.get("replies"))
    quotes = _coerce_int(target.get("quote_count") or target.get("quotes"))
    age = _age_hours(target)
    raw = likes + 2 * retweets + 0.5 * replies + 3 * quotes
    return raw / max(age, 1.0)


def _is_op(target: Dict[str, Any]) -> bool:
    """Return True when the target looks like an OP (not a reply-chain post).

    Tolerant: missing flags default to OP rather than dropping. Explicit
    ``is_reply=True`` or ``in_reply_to_status_id_str`` set is the kill signal.
    Reply signals win over a stale ``is_op=True`` so a target that carries
    both (e.g. an upstream scanner sets is_op then a follow-up enrichment
    flips is_reply) is correctly dropped.
    """
    if target.get("is_reply") is True:
        return False
    reply_id = target.get("in_reply_to_status_id_str") or target.get("in_reply_to_status_id")
    if reply_id:
        return False
    if target.get("is_op") is False:
        return False
    return True


def _is_english(target: Dict[str, Any]) -> bool:
    lang = target.get("lang")
    if lang is None:
        # Tolerant default — Twttr241 doesn't always populate lang.
        return True
    return str(lang).lower() in ("en", "en-us", "en-gb")


def _is_retweet(target: Dict[str, Any]) -> bool:
    if target.get("is_retweet") is True:
        return True
    text = (target.get("content_snippet") or target.get("text") or "").lstrip()
    if text.startswith("RT @"):
        return True
    return False


def rank_x_targets(
    targets: Iterable[Dict[str, Any]],
    replied_authors_window_hours: int = DEFAULT_WINDOW_HOURS,
    replied_authors: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Filter and sort X targets by engagement.

    Args:
        targets: iterable of candidate target dicts from ``_scan_x``.
        replied_authors_window_hours: dedupe window for already-replied authors.
            Default 7 days per founder directive.
        replied_authors: explicit author set (lowercase, no leading ``@``).
            When ``None``, the set is read from ``~/.delimit/social_log.jsonl``.
            Tests inject an explicit set to avoid touching disk.

    Returns:
        A new list sorted by engagement score DESC. Each item gets a
        ``_rank_score`` key for downstream observability. Filtered items are
        dropped (not kept with score=0) so the caller can blindly slice the
        first N.
    """
    if replied_authors is None:
        replied_authors = _replied_authors_within(replied_authors_window_hours)
    else:
        replied_authors = {_normalize_handle(a) for a in replied_authors}

    survivors: List[Dict[str, Any]] = []
    for t in targets or []:
        if not isinstance(t, dict):
            continue
        if t.get("error"):
            continue

        # 1. is_op
        if not _is_op(t):
            continue
        # 2. lang == 'en' (tolerant of missing field)
        if not _is_english(t):
            continue
        # 3. drop retweets
        if _is_retweet(t):
            continue
        # 4. dedupe authors we replied to in window
        author_norm = _normalize_handle(t.get("author"))
        if author_norm and author_norm in replied_authors:
            continue

        scored = dict(t)
        scored["_rank_score"] = round(score_target(t), 4)
        survivors.append(scored)

    # 5. sort score DESC, stable
    survivors.sort(key=lambda x: x.get("_rank_score", 0.0), reverse=True)
    return survivors


__all__ = [
    "DEFAULT_WINDOW_HOURS",
    "score_target",
    "rank_x_targets",
]
