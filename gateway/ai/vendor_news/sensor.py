"""Vendor-news sensor (LED-1250).

Polls watchlisted vendor accounts on X for fresh, high-engagement posts
that Delimit can riff on. Reuses the existing twttr241 fetch path from
``ai.social_target`` so the LRU + SQLite cache + budget gate are honored
without duplication.

Output is a structured dict consumed by ``ai.vendor_news.drafter``:

    {
        "triggered": [tweet_obj, ...],
        "skipped": [{tweet_id, reason}, ...],
        "errors":  [{handle, error}, ...],
        "stats":   {accounts_polled, posts_seen, ...},
    }

Each ``tweet_obj`` carries the metadata the drafter needs to compose a
riff (vendor name, product list, source URL, metrics, age) without
re-walking the twttr241 response shape.

Budget control: ``DELIMIT_VENDOR_NEWS_BUDGET`` env var (default 50)
caps the number of *live* twttr241 calls per run. Cache hits are free
and do not count against the budget.

Logs every run as a JSONL line at ``~/.delimit/vendor_news_sensor.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── paths / config ────────────────────────────────────────────────────

_PKG_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = _PKG_DIR / "watchlist.yaml"

SENSOR_LOG_PATH = Path.home() / ".delimit" / "vendor_news_sensor.jsonl"

# Default per-run budget (live twttr241 calls). Overridable via env.
DEFAULT_RUN_BUDGET = 50

# Per-account fetch size. We only need recent tweets; small page = small
# response = lower processing cost.
DEFAULT_PER_ACCOUNT_LIMIT = 10


# ── watchlist loader ──────────────────────────────────────────────────


def load_watchlist(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the watchlist YAML. Returns the parsed dict.

    Falls back to a tiny built-in default if the file is missing OR
    PyYAML is unavailable, so the sensor never hard-crashes on a bad
    install. Callers should treat the return value as read-only.
    """
    p = Path(path) if path else WATCHLIST_PATH
    default: Dict[str, Any] = {
        "version": 1,
        "accounts": [],
        "trigger_thresholds": {
            "min_likes": 200,
            "min_retweets": 30,
            "min_quotes": 15,
            "velocity_likes_per_hour": 100,
            "max_age_hours": 4,
        },
        "no_at_mention": True,
    }
    if not p.exists():
        logger.warning("vendor_news watchlist missing: %s (using empty default)", p)
        return default
    try:
        import yaml  # local import — optional dep
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            return default
        # Merge defaults for any missing top-level keys.
        for k, v in default.items():
            loaded.setdefault(k, v)
        # Ensure trigger_thresholds has all expected keys.
        thresh = dict(default["trigger_thresholds"])
        thresh.update(loaded.get("trigger_thresholds") or {})
        loaded["trigger_thresholds"] = thresh
        return loaded
    except Exception as exc:  # pragma: no cover — yaml/parse failures
        logger.warning("vendor_news watchlist parse failed (%s); using default", exc)
        return default


# ── tweet-time parsing ────────────────────────────────────────────────


_TWITTER_TS_FORMATS = (
    # "Wed Apr 24 18:30:01 +0000 2026" — twttr241 legacy.created_at
    "%a %b %d %H:%M:%S %z %Y",
)


def _parse_created_at(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    s = raw.strip()
    # ISO-8601 (cache write paths sometimes normalize).
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    for fmt in _TWITTER_TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _age_hours(created: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    cur = now or datetime.now(timezone.utc)
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    delta = cur - created
    return max(0.0, delta.total_seconds() / 3600.0)


# ── twttr241 fetch (per-account search-v2 with from:HANDLE) ───────────


def _build_from_query(handle: str) -> str:
    """Twitter search syntax for "tweets authored by @handle".

    We use ``search-v2`` because the existing twttr241 wrapper already
    routes that endpoint through the cache + budget gate. ``from:`` is
    a standard Twitter search operator; combined with a fresh sort we
    get the same shape we already parse in social_target._scan_x_twttr.
    """
    h = (handle or "").lstrip("@").strip()
    return f"from:{h}" if h else ""


def _fetch_account_recent(
    handle: str,
    limit: int = DEFAULT_PER_ACCOUNT_LIMIT,
    venture_tag: str = "delimit",
) -> Dict[str, Any]:
    """Fetch recent tweets authored by ``handle``.

    Returns a dict:
        {
            "tweets": [normalized_tweet, ...],
            "from_cache": bool,
            "budget_exceeded": bool,
            "mode": str | None,
            "error": str | None,
        }
    Never raises. Reuses the cache+budget chain from social_target.
    """
    out: Dict[str, Any] = {
        "tweets": [],
        "from_cache": False,
        "budget_exceeded": False,
        "mode": None,
        "error": None,
    }

    # Lazy import to keep the module import cheap (and to allow tests
    # to monkeypatch the underlying functions).
    try:
        from ai.social_target import (
            _twttr_fetch,
            _get_rapidapi_key,
            _extract_tweets_for_corpus,
        )
    except Exception as exc:  # pragma: no cover — import failure is fatal upstream
        out["error"] = f"social_target import failed: {exc}"
        return out

    api_key = _get_rapidapi_key()
    if not api_key:
        out["error"] = "rapidapi key not configured"
        return out

    query = _build_from_query(handle)
    if not query:
        out["error"] = f"invalid handle: {handle!r}"
        return out

    encoded = urllib.parse.quote(query)
    url = (
        f"https://twitter241.p.rapidapi.com/search-v2"
        f"?query={encoded}&type=Latest&count={int(limit)}"
    )
    endpoint = "search/latest"
    params = {"query": query, "type": "Latest", "count": int(limit)}

    fetched = _twttr_fetch(
        endpoint=endpoint,
        params=params,
        url=url,
        api_key=api_key,
        handle=handle,
        venture_tag=venture_tag,
        extract_tweets=_extract_tweets_for_corpus,
    )

    out["from_cache"] = bool(fetched.get("from_cache"))
    out["budget_exceeded"] = bool(fetched.get("budget_exceeded"))
    out["mode"] = fetched.get("mode")

    response = fetched.get("response")
    if response is None:
        out["error"] = fetched.get("error") or out["mode"] or "no response"
        return out

    out["tweets"] = _normalize_search_response(response, expected_handle=handle)
    return out


def _normalize_search_response(response: Dict[str, Any], expected_handle: str) -> List[Dict[str, Any]]:
    """Walk the twttr241 search-v2 response and return per-tweet dicts.

    Mirrors the navigation in ``social_target._scan_x_twttr`` but produces
    a flat shape tuned to the riff drafter (id, text, author, metrics,
    created_at). Filters out retweets and replies — only original posts
    from the watched handle are eligible to ride the news cycle.
    """
    tweets: List[Dict[str, Any]] = []
    expected = (expected_handle or "").lstrip("@").lower()
    try:
        instructions = (
            (response or {})
            .get("result", {})
            .get("timeline", {})
            .get("instructions", [])
        )
    except Exception:
        return tweets

    for instruction in instructions:
        for entry in instruction.get("entries", []) or []:
            tweet_result = (
                (entry.get("content") or {})
                .get("itemContent", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            if not tweet_result:
                continue
            legacy = tweet_result.get("legacy") or {}
            core = tweet_result.get("core") or {}
            user_legacy = (
                core.get("user_results", {}).get("result", {}).get("legacy") or {}
            )
            user_core = (
                core.get("user_results", {}).get("result", {}).get("core") or {}
            )

            tid = legacy.get("id_str") or ""
            if not tid:
                continue

            screen_name = (
                user_core.get("screen_name")
                or user_legacy.get("screen_name")
                or ""
            )
            if expected and screen_name.lower() != expected:
                # Search-v2 with from:HANDLE should always match, but
                # defend against the API echoing quoted/retweeted authors.
                continue

            full_text = legacy.get("full_text") or legacy.get("text") or ""
            in_reply_to = legacy.get("in_reply_to_status_id_str") or ""
            is_retweet = bool(legacy.get("retweeted_status_result")) or full_text.lstrip().startswith("RT @")
            if in_reply_to or is_retweet:
                # Only ride original posts — replies and RTs aren't news.
                continue

            metrics = {
                "favorite_count": int(legacy.get("favorite_count") or 0),
                "retweet_count": int(legacy.get("retweet_count") or 0),
                "reply_count": int(legacy.get("reply_count") or 0),
                "quote_count": int(legacy.get("quote_count") or 0),
                "view_count": int(((tweet_result.get("views") or {}).get("count")) or 0),
            }

            tweets.append(
                {
                    "id": str(tid),
                    "text": full_text,
                    "author": screen_name,
                    "created_at": legacy.get("created_at", ""),
                    "metrics": metrics,
                    "url": (
                        f"https://x.com/{screen_name}/status/{tid}"
                        if screen_name
                        else f"https://x.com/i/status/{tid}"
                    ),
                }
            )
    return tweets


# ── trigger logic ─────────────────────────────────────────────────────


def _meets_trigger(
    metrics: Dict[str, Any],
    age_hours: Optional[float],
    thresholds: Dict[str, Any],
) -> Tuple[bool, str]:
    """Decide whether a tweet's metrics + age cross any trigger.

    Returns (passed, reason). ``reason`` is a short tag suitable for
    logging (``min_likes`` / ``velocity`` / ``min_retweets`` etc.) so we
    can audit which heuristic fired in production.
    """
    likes = int(metrics.get("favorite_count") or 0)
    retweets = int(metrics.get("retweet_count") or 0)
    quotes = int(metrics.get("quote_count") or 0)

    max_age = float(thresholds.get("max_age_hours", 4))
    if age_hours is not None and age_hours > max_age:
        return (False, f"too_old:{age_hours:.1f}h")

    min_likes = int(thresholds.get("min_likes", 200))
    min_rt = int(thresholds.get("min_retweets", 30))
    min_q = int(thresholds.get("min_quotes", 15))
    velocity = float(thresholds.get("velocity_likes_per_hour", 100))

    if likes >= min_likes:
        return (True, "min_likes")
    if retweets >= min_rt:
        return (True, "min_retweets")
    if quotes >= min_q:
        return (True, "min_quotes")
    # Velocity: only meaningful for fresh posts. Avoid divide-by-zero
    # by clamping the floor to 0.25h (15min). Posts younger than that
    # rarely have stable metrics anyway.
    if age_hours is not None:
        denom = max(0.25, age_hours)
        if (likes / denom) >= velocity:
            return (True, "velocity")
    return (False, "below_thresholds")


# ── log helpers ───────────────────────────────────────────────────────


def _append_sensor_log(payload: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path) if path else SENSOR_LOG_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort logging
        logger.warning("vendor_news sensor log write failed: %s", exc)


# ── main entry ────────────────────────────────────────────────────────


def scan_vendor_news(
    dry_run: bool = False,
    watchlist_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Scan watchlisted accounts for fresh high-engagement posts.

    Args:
        dry_run: When True, returns the same shape but suppresses the
            JSONL log write. Useful for tests + ad-hoc inspection.
        watchlist_path: Override the watchlist YAML location. Defaults
            to ``ai/vendor_news/watchlist.yaml``.
        log_path: Override the sensor JSONL log path.
        now: Optional override for "current time" — exposed so tests
            can pin the clock and exercise the age window deterministically.

    Returns:
        Dict with ``triggered``, ``skipped``, ``errors``, ``stats``.
        ``triggered`` entries are ready to feed into ``draft_vendor_riff``.
    """
    cfg = load_watchlist(watchlist_path)
    accounts = cfg.get("accounts") or []
    thresholds = cfg.get("trigger_thresholds") or {}

    budget = int(os.environ.get("DELIMIT_VENDOR_NEWS_BUDGET", DEFAULT_RUN_BUDGET))
    live_calls = 0

    triggered: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    posts_seen = 0

    cur_now = now or datetime.now(timezone.utc)

    for account in accounts:
        handle = (account or {}).get("handle", "")
        if not handle:
            continue
        vendor = account.get("vendor", "")
        products = list(account.get("products") or [])

        if live_calls >= budget:
            skipped.append({"handle": handle, "reason": "budget_exhausted"})
            continue

        try:
            fetched = _fetch_account_recent(handle=handle)
        except Exception as exc:  # pragma: no cover — defensive
            errors.append({"handle": handle, "error": f"fetch_exc: {exc}"})
            continue

        # Only count live HTTP calls against the budget. Cache hits are free.
        if not fetched.get("from_cache"):
            live_calls += 1

        if fetched.get("error"):
            errors.append({"handle": handle, "error": fetched["error"]})
            continue

        if fetched.get("budget_exceeded"):
            errors.append({"handle": handle, "error": f"twttr241_budget:{fetched.get('mode')}"})
            continue

        for tw in fetched.get("tweets") or []:
            posts_seen += 1
            metrics = tw.get("metrics") or {}
            age = _age_hours(_parse_created_at(tw.get("created_at", "")), now=cur_now)
            passed, reason = _meets_trigger(metrics, age, thresholds)

            entry = {
                "id": tw.get("id"),
                "text": tw.get("text", ""),
                "author": tw.get("author", ""),
                "url": tw.get("url", ""),
                "created_at": tw.get("created_at", ""),
                "age_hours": age,
                "metrics": metrics,
                "vendor": vendor,
                "products": products,
                "trigger_reason": reason,
            }

            if passed:
                triggered.append(entry)
            else:
                skipped.append({
                    "id": tw.get("id"),
                    "handle": handle,
                    "reason": reason,
                    "metrics": metrics,
                    "age_hours": age,
                })

    stats = {
        "ts": cur_now.isoformat(),
        "accounts_polled": len(accounts),
        "posts_seen": posts_seen,
        "posts_triggered": len(triggered),
        "posts_skipped": len(skipped),
        "errors": len(errors),
        "live_calls": live_calls,
        "budget": budget,
        "dry_run": dry_run,
    }

    result = {
        "triggered": triggered,
        "skipped": skipped,
        "errors": errors,
        "stats": stats,
    }

    if not dry_run:
        # Persist per-run summary (NOT the full triggered list — that
        # can balloon the log file). Sample a few ids so we can grep
        # for a specific post if a riff later misbehaves.
        log_payload = dict(stats)
        log_payload["triggered_ids"] = [t["id"] for t in triggered[:25]]
        log_payload["error_handles"] = [e["handle"] for e in errors[:25]]
        _append_sensor_log(log_payload, path=log_path)

    return result


__all__ = [
    "DEFAULT_RUN_BUDGET",
    "SENSOR_LOG_PATH",
    "WATCHLIST_PATH",
    "load_watchlist",
    "scan_vendor_news",
]
