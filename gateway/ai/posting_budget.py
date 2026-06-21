"""Global daily posting budget for the @delimit_ai brand account (2026-06-19).

Founder-ratified with the deliberation panel's guardrails. A single HARD cap of
``DAILY_POST_CAP`` brand tweets per UTC day, shared across ALL three autopost
sources (ship_event, vendor_news_riff, scheduled_original). The cap is counted
from tweets ACTUALLY POSTED, not queued — the canonical source is the
posted-tweet log the posting path writes (``social_log.jsonl`` via
``ai.social.log_post``; every ``post_tweet`` success appends a row with a UTC
``ts`` and ``platform``).

Why social_log.jsonl is the source of truth:
  * ``ai.social.post_tweet`` calls ``log_post("twitter", ...)`` on every
    successful create_tweet, writing ``{"ts": <UTC ISO>, "platform":
    "twitter", "handle", "post_id", ...}``.
  * ``content_engine.post_next_tweet`` (the cron that drains tweet_queue.json)
    posts via ``post_tweet``, so its sends are logged there too.
  * The queue's ``posted=true`` entries are a SECONDARY signal (they can lag /
    be edited), so we count the posted-tweet LOG, not the queue.

Priority ordering under the cap (panel guardrail):
  * ship_event and vendor_news_riff are P0 (kept).
  * scheduled_original is P2 (lower) — a busy ship/vendor day naturally drops
    scheduled originals first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Canonical posted-tweet log (written by ai.social.log_post on every send).
POSTED_LOG_PATH = Path.home() / ".delimit" / "social_log.jsonl"

# HARD global cap: brand tweets actually posted per UTC day, across all 3
# sources. Founder-settled cadence (2026-06-19): GLOBAL 6/DAY cap, with a
# 4/day scheduled-originals baseline leaving headroom for ~2 ship/vendor riffs.
# Configurable via env for ops (DELIMIT_DAILY_POST_CAP) so the value can be
# retuned without a code change.
import os as _os

DAILY_POST_CAP = int(_os.environ.get("DELIMIT_DAILY_POST_CAP", "6"))

# Priority ranks used when picking the next queue entry to post. Lower number
# = higher priority (posted first). Unknown categories sort after P0 but
# before P2 scheduled originals so legacy queue entries are not starved.
_CATEGORY_PRIORITY = {
    "ship_event": 0,
    "vendor_news_riff": 0,
    "scheduled_original": 2,
}
_DEFAULT_PRIORITY = 1


def _platform_is_twitter(entry: dict) -> bool:
    return (entry.get("platform") or "").strip().lower() in ("twitter", "x")


def posts_today(
    now: Optional[datetime] = None,
    *,
    posted_log_path: Optional[Path] = None,
) -> int:
    """Count brand tweets ACTUALLY POSTED today (UTC) from the posted-tweet log.

    Counts only twitter/x rows (Reddit/HN/devto live in the same log but are a
    different surface and budget). A row counts when its ``ts`` falls on the
    same UTC calendar day as ``now``. Graceful on any read error (returns 0 so
    a missing/corrupt log never hard-blocks posting; the existing
    ``should_post_now`` 24/day backstop still applies upstream)."""
    cur = now or datetime.now(timezone.utc)
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    cur_utc = cur.astimezone(timezone.utc)
    today = cur_utc.date()

    path = posted_log_path or POSTED_LOG_PATH
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict) or not _platform_is_twitter(entry):
                continue
            ts_str = entry.get("ts") or ""
            if not ts_str:
                continue
            try:
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone(timezone.utc).date() == today:
                count += 1
    except OSError:
        return 0
    return count


def remaining_today(
    now: Optional[datetime] = None,
    *,
    posted_log_path: Optional[Path] = None,
    cap: Optional[int] = None,
) -> int:
    """Posts remaining under the global cap today (never negative)."""
    c = DAILY_POST_CAP if cap is None else cap
    return max(0, c - posts_today(now, posted_log_path=posted_log_path))


def cap_reached(
    now: Optional[datetime] = None,
    *,
    posted_log_path: Optional[Path] = None,
    cap: Optional[int] = None,
) -> bool:
    """True iff the global daily cap is hit (post nothing further today)."""
    c = DAILY_POST_CAP if cap is None else cap
    return posts_today(now, posted_log_path=posted_log_path) >= c


def category_priority(category: str) -> int:
    """Return the priority rank for a queue category (lower = posted first)."""
    return _CATEGORY_PRIORITY.get((category or "").strip().lower(), _DEFAULT_PRIORITY)


def unposted_queue_count(queue) -> int:
    """Count unposted entries in a loaded tweet_queue list (defensive)."""
    if not isinstance(queue, list):
        return 0
    return sum(1 for e in queue if isinstance(e, dict) and not e.get("posted"))


__all__ = [
    "DAILY_POST_CAP",
    "POSTED_LOG_PATH",
    "posts_today",
    "remaining_today",
    "cap_reached",
    "category_priority",
    "unposted_queue_count",
]
