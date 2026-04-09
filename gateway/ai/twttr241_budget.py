"""
Twttr241 budget gate — single entry point for every HTTP call to Twttr241.

Enforces tiered breakers from DECISION_TWTTR241_CORPUS.md and fires
delimit_notify + delimit_evidence_collect (once per tier per hour bucket)
on the first trip into a degraded tier.

Tiers (based on TweetCorpus.check_budget() mode):
  normal             <70%      allowed everywhere
  throttle           70-85%    blocks THROTTLED_ENDPOINTS
  founder-priority   85-95%    blocks non-founder handles
  hard-stop          >=95%     blocks everything
  daily-freeze       day>=3200 blocks everything
  monthly-freeze     mo>=95000 blocks everything

Usage:
    from ai.twttr241_budget import budget_gate, record_call, BudgetExceeded

    try:
        budget_gate("user/timeline", handle="delimit_ai")
    except BudgetExceeded as e:
        return {"error": "budget", "mode": e.mode}
    resp = http.get(...)
    record_call("user/timeline", success=resp.ok)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- constants

# Founder-priority handles — when mode == "founder-priority", only these are allowed.
FOUNDER_PRIORITY_HANDLES = frozenset(
    {
        "delimit_ai",
        "wire_report",
        "domainvested",
        "livetube_ai",
        "stake_one",
    }
)

# Throttled endpoints — disabled when mode == "throttle".
THROTTLED_ENDPOINTS = frozenset(
    {
        "search/latest",
    }
)

# Tiers that are allowed to proceed (possibly with per-call filtering).
_ALLOWED_MODES = frozenset({"normal", "throttle", "founder-priority"})

# Tiers that represent a degraded state and should emit a trip notification
# the first time we see them in a given hour bucket.
_TRIP_TIERS = frozenset(
    {"throttle", "founder-priority", "hard-stop", "daily-freeze", "monthly-freeze"}
)

_DEDUP_PATH = Path.home() / ".delimit" / "state" / "twttr241_trip_dedup.json"


# ---------------------------------------------------------------- exceptions


class BudgetExceeded(Exception):
    """Raised when the budget gate refuses a call.

    Not a bug — signals graceful degradation. Callers should catch this and
    skip the underlying HTTP request.
    """

    def __init__(self, mode: str, detail: Dict[str, Any]):
        self.mode = mode
        self.detail = detail
        super().__init__(f"twttr241 budget gate: {mode}")


# ---------------------------------------------------------------- corpus singleton

_corpus_singleton: Any = None


def _get_corpus(corpus: Optional[Any] = None) -> Any:
    """Return the provided corpus or lazily build a singleton TweetCorpus."""
    global _corpus_singleton
    if corpus is not None:
        return corpus
    if _corpus_singleton is None:
        from ai.tweet_corpus import TweetCorpus  # local import to allow test patching

        _corpus_singleton = TweetCorpus()
    return _corpus_singleton


def _reset_corpus_singleton() -> None:
    """Test hook — clears the lazy TweetCorpus singleton."""
    global _corpus_singleton
    _corpus_singleton = None


# ---------------------------------------------------------------- dedup state


def _current_hour_bucket() -> int:
    return int(time.time()) // 3600


def _load_dedup_state() -> Dict[str, Any]:
    try:
        with open(_DEDUP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"hour_bucket": _current_hour_bucket(), "tripped_tiers": []}
    if not isinstance(data, dict):
        return {"hour_bucket": _current_hour_bucket(), "tripped_tiers": []}
    data.setdefault("hour_bucket", _current_hour_bucket())
    data.setdefault("tripped_tiers", [])
    if not isinstance(data["tripped_tiers"], list):
        data["tripped_tiers"] = []
    return data


def _save_dedup_state(state: Dict[str, Any]) -> None:
    try:
        _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _DEDUP_PATH)
    except OSError as e:
        logger.warning("twttr241_budget: failed to persist dedup state: %s", e)


def _should_notify_trip(mode: str) -> bool:
    """Check + update dedup state. Returns True if this is the first trip
    for (mode, current_hour_bucket)."""
    if mode not in _TRIP_TIERS:
        return False
    state = _load_dedup_state()
    now_bucket = _current_hour_bucket()
    if state.get("hour_bucket") != now_bucket:
        state = {"hour_bucket": now_bucket, "tripped_tiers": []}
    if mode in state["tripped_tiers"]:
        _save_dedup_state(state)
        return False
    state["tripped_tiers"].append(mode)
    _save_dedup_state(state)
    return True


# ---------------------------------------------------------------- notify + evidence


def _fire_trip_notification(mode: str, budget: Dict[str, Any], endpoint: str, handle: Optional[str]) -> None:
    """Fire delimit_notify + delimit_evidence_collect. Swallows all errors."""
    hour_req = budget.get("hour_requests", 0)
    day_req = budget.get("day_requests", 0)
    month_req = budget.get("month_requests", 0)
    usage_pct = budget.get("usage_pct", 0.0)

    subject = f"[ALERT] Twttr241 budget trip: {mode}"
    body_lines = [
        f"Twttr241 budget gate entered tier: {mode}",
        "",
        f"  endpoint: {endpoint}",
        f"  handle:   {handle or '(none)'}",
        "",
        f"  hour_requests:  {hour_req}",
        f"  day_requests:   {day_req}",
        f"  month_requests: {month_req}",
        f"  usage_pct:      {usage_pct:.2%}",
        "",
        "Graceful degradation is active. See DECISION_TWTTR241_CORPUS.md.",
    ]
    body = "\n".join(body_lines)

    try:
        from ai.notify import send_notification

        send_notification(
            channel="email",
            subject=subject,
            message=body,
            event_type="twttr241_budget_trip",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("twttr241_budget: notify failed: %s", e)

    try:
        from ai.backends.repo_bridge import evidence_collect

        evidence_collect(
            target=str(Path(__file__).resolve().parents[1]),
            options={
                "evidence_type": "budget_trip",
                "mode": mode,
                "endpoint": endpoint,
                "handle": handle,
                "budget": budget,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("twttr241_budget: evidence_collect failed: %s", e)


# ---------------------------------------------------------------- gate


def budget_gate(
    endpoint: str,
    handle: Optional[str] = None,
    corpus: Optional[Any] = None,
) -> Dict[str, Any]:
    """Single budget gate. Call BEFORE every twttr241 HTTP request.

    Returns the budget dict from check_budget() on success.
    Raises BudgetExceeded if the call must not proceed.

    Fires delimit_notify + delimit_evidence_collect once per (hour_bucket, tier)
    on first trip into a degraded tier.
    """
    c = _get_corpus(corpus)
    budget = c.check_budget()
    mode = budget.get("mode", "normal")

    # Hard denials.
    if mode in ("hard-stop", "daily-freeze", "monthly-freeze"):
        if _should_notify_trip(mode):
            _fire_trip_notification(mode, budget, endpoint, handle)
        raise BudgetExceeded(mode, budget)

    if mode == "founder-priority":
        trip_fired = _should_notify_trip(mode)
        if trip_fired:
            _fire_trip_notification(mode, budget, endpoint, handle)
        normalized_handle = (handle or "").lstrip("@").lower()
        allowed_handles = {h.lower() for h in FOUNDER_PRIORITY_HANDLES}
        if normalized_handle not in allowed_handles:
            raise BudgetExceeded(mode, budget)
        return budget

    if mode == "throttle":
        trip_fired = _should_notify_trip(mode)
        if trip_fired:
            _fire_trip_notification(mode, budget, endpoint, handle)
        if endpoint in THROTTLED_ENDPOINTS:
            raise BudgetExceeded(mode, budget)
        return budget

    if mode not in _ALLOWED_MODES:
        # Unknown mode — fail closed.
        raise BudgetExceeded(mode, budget)

    return budget


def record_call(
    endpoint: str,
    success: bool,
    corpus: Optional[Any] = None,
    status: Optional[int] = None,
) -> None:
    """Call AFTER every twttr241 HTTP request to increment the budget counter."""
    c = _get_corpus(corpus)
    try:
        c.record_api_call(endpoint, success, status=status)
    except TypeError:
        # Older TweetCorpus signatures without `status=` kwarg.
        c.record_api_call(endpoint, success)


__all__ = [
    "FOUNDER_PRIORITY_HANDLES",
    "THROTTLED_ENDPOINTS",
    "BudgetExceeded",
    "budget_gate",
    "record_call",
]
