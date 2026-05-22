"""Delimit-fit floor (LED-1240 part B — selectivity bar, 2026-05-05).

The validator (``capability_validator``) hard-fails generic-claim drafts
AFTER the LLM has generated them. That catches drift but burns tokens and
surfaces weak drafts to founder review. This module pushes the bar
upstream: a thread/target must pass the Delimit-fit floor BEFORE we even
draft for it. If the floor rejects, we abstain. Abstain > weak draft.

The fit floor accepts a thread when ANY of these holds:

  1. The text matches a **Delimit-domain signal** (openapi, breaking change,
     semver, schema diff, merge gate, mcp server, ci governance, soc 2, …).
  2. The text matches an **AI-coding-orbit signal** (claude code, codex cli,
     cursor, gemini cli, mcp tool, …) AND a **technical-context word**
     (ship, merge, deploy, release, PR, review, audit, test, …). Orbit-only
     mentions without technical context (e.g. "I love Claude Code!") are
     not enough — that's the signal that produces noise drafts.
  3. The engagement score is high enough that abstaining has clear
     opportunity cost (default ≥ 50). High-engagement-but-off-topic threads
     pass through with ``human_only=True`` so the orchestrator can choose
     to surface them for human review without auto-drafting.

A failing thread returns a dict with ``reason="no_delimit_fit"`` and is
NOT drafted. Logging is the caller's job — this module is pure logic.

Topic-coverage cooldown (``recent_topic_drafted``) is a separate function
that consults ``social_log.jsonl`` (the same source ``x_ranker`` uses for
author dedupe). If we drafted on the same topic within the cooldown
window (default 7 days), the next thread on that topic is rejected with
``reason="topic_cooldown"``.

Centralizing this logic here means ``x_ranker`` (X candidate ordering) and
``social_target`` (cross-platform target processing) both consume the same
gate — no drift between surfaces.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Default cooldown window for topic coverage.
# Original founder directive (2026-05-05): 7 days to avoid spam-pattern detection.
# Revised 2026-05-12 (LED-1356, panel-unanimous): 7 days × cross-platform global
# scope was producing 57% abstention rate (115/200 recent abstentions =
# topic_cooldown) and effectively blocked every Delimit-relevant topic for a
# full week after one draft. Reduced to 48h AND scoped per-platform/per-subreddit
# so a Reddit/r/mcp draft no longer cools an HN draft on MCP, and a Reddit/r/mcp
# draft no longer cools a Reddit/r/programming draft on MCP. Different audiences,
# different scans.
DEFAULT_COOLDOWN_HOURS = 48
# Backwards-compat alias for any callers that still pass `cooldown_days=` —
# converted to hours internally. Don't add new callers using this name.
DEFAULT_COOLDOWN_DAYS = DEFAULT_COOLDOWN_HOURS / 24

# Default high-engagement opportunity-cost threshold. Threads above this score
# pass the fit floor even without keyword match, but with ``human_only=True``
# so they don't auto-draft.
DEFAULT_HIGH_ENGAGEMENT_FLOOR = 50.0

# Same SOCIAL_LOG path as ai.x_ranker — single source of truth for "what we
# recently posted to". Indirection via a getter so tests can monkeypatch.
SOCIAL_LOG = Path.home() / ".delimit" / "social_log.jsonl"


def _social_log_path() -> Path:
    """Indirection for tests."""
    return SOCIAL_LOG


# ── Signal vocabularies ──────────────────────────────────────────────

# Direct Delimit-domain signals. These are the canonical surfaces the
# product addresses. Any one of these is sufficient by itself.
#
# Keep this list narrow and concrete — it's the difference between
# "drafts that have something to say" and "drafts that name-drop a
# topic". Per the LED-1240 directive: abstain > weak draft.
DELIMIT_DOMAIN_SIGNALS = (
    # API governance
    r"\bopenapi\b",
    r"\bopen[\s-]?api\b",
    r"\bapi[\s-]?versioning\b",
    r"\bbreaking[\s-]?change(?:s)?\b",
    r"\bsemver\b",
    r"\bsemantic[\s-]?versioning\b",
    r"\bschema[\s-]?diff\b",
    r"\bspec[\s-]?validation\b",
    r"\bapi[\s-]?spec(?:s)?\b",
    r"\bapi[\s-]?contract(?:s)?\b",
    r"\bapi[\s-]?migration(?:s)?\b",
    r"\brelease[\s-]?notes\b",
    # Merge / governance
    r"\bmerge[\s-]?gate\b",
    r"\bci[\s-]?governance\b",
    r"\bapi[\s-]?governance\b",
    # MCP
    r"\bmcp[\s-]?server(?:s)?\b",
    r"\bmodel[\s-]?context[\s-]?protocol\b",
    # Compliance / audit
    r"\bsoc[\s-]?2\b",
    r"\bcompliance\b",
    r"\bauditor(?:s)?\b",
    r"\battestation(?:s)?\b",
    r"\baudit[\s-]?trail\b",
)

# Adjacent / orbit signals — these are common in the AI-coding-assistant
# ecosystem we live in. By themselves they're noise; combined with a
# technical-context word they're a real signal.
ORBIT_SIGNALS = (
    r"\bclaude[\s-]?code\b",
    r"\bcodex(?:[\s-]?cli)?\b",
    r"\bcursor(?:[\s-]?ai)?\b",
    r"\bgemini[\s-]?cli\b",
    r"\bmcp[\s-]?tool(?:s)?\b",
    r"\bmcp\b",
    r"\bai[\s-]?coding[\s-]?assistant(?:s)?\b",
    r"\bai[\s-]?agent(?:s)?\b",
    r"\bai[\s-]?coder(?:s)?\b",
)

# Technical-context words — these qualify an orbit mention as real signal.
# The list is what an engineer would actually say when describing an
# integration / shipping / review / audit moment.
TECH_CONTEXT_SIGNALS = (
    r"\bship(?:s|ped|ping)?\b",
    r"\bmerg(?:e|ed|es|ing)\b",
    r"\bdeploy(?:s|ed|ing|ment)?\b",
    r"\brelease(?:s|d|ing)?\b",
    r"\bpr(?:s)?\b",
    r"\bpull[\s-]?request(?:s)?\b",
    r"\breview(?:s|ed|ing)?\b",
    r"\baudit(?:s|ed|ing)?\b",
    r"\btest(?:s|ed|ing)?\b",
    r"\bci(?:/cd)?\b",
    r"\bgithub[\s-]?action(?:s)?\b",
    r"\bspec(?:s)?\b",
    r"\bschema\b",
    r"\bbreaking\b",
    r"\bregression(?:s)?\b",
)


# Compiled at module load — case-insensitive, matched against full text.
_DELIMIT_DOMAIN_RE = re.compile("|".join(DELIMIT_DOMAIN_SIGNALS), re.IGNORECASE)
_ORBIT_RE = re.compile("|".join(ORBIT_SIGNALS), re.IGNORECASE)
_TECH_CONTEXT_RE = re.compile("|".join(TECH_CONTEXT_SIGNALS), re.IGNORECASE)


# ── Topic extraction (for cooldown) ─────────────────────────────────

# Lightweight topic fingerprint: the union of matched Delimit-domain signals
# + matched orbit signals (lowercased, deduped). If two threads share a
# matched signal AND we drafted on one inside the cooldown window, the
# second one is rejected. This avoids 5-tweets-on-MCP-in-3-days drafts.
def _extract_topic_fingerprint(text: str) -> Set[str]:
    """Return the set of canonical signal terms present in ``text``.

    Used as the key for the topic-coverage cooldown. Empty string and
    ``None`` return an empty set — those threads can never collide.
    """
    if not text:
        return set()
    out: Set[str] = set()
    for m in _DELIMIT_DOMAIN_RE.finditer(text):
        out.add(m.group(0).lower())
    for m in _ORBIT_RE.finditer(text):
        out.add(m.group(0).lower())
    return out


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


def _log_entry_scope_key(entry: dict) -> str:
    """LED-1356 platform-scoped cooldown key. Reddit cooldowns are per-subreddit
    (different subreddits = different audiences); HN/devto/X are global within
    the platform. Cross-platform doesn't cool.

    Returns:
        - "reddit:r/<name>" for Reddit entries with a subreddit field
        - "reddit" for Reddit entries without (legacy fallback)
        - The platform name itself for hn / devto / x / twitter
        - "unknown" when the entry doesn't carry a platform
    """
    platform = (entry.get("platform") or "").lower()
    if not platform:
        return "unknown"
    if platform == "reddit":
        sub = (entry.get("subreddit") or "").lower()
        if sub:
            sub = sub.lstrip("r/")
            return f"reddit:r/{sub}"
        return "reddit"
    # twitter and x are aliases for the same audience
    if platform == "twitter":
        return "x"
    return platform


def _recent_topic_fingerprints(
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    log_path: Optional[Path] = None,
    cooldown_days: Optional[float] = None,
) -> Dict[str, Set[str]]:
    """Return platform-scoped topic fingerprints from ``social_log.jsonl``
    within the cooldown window.

    LED-1356 changes (2026-05-12, panel-unanimous):
    - Window default 7d → 48h.
    - Return shape Set[str] → Dict[scope_key, Set[str]] so callers can ask
      "what topics has THIS audience seen recently" instead of the prior
      "global union of every topic across every platform."
    - `cooldown_days` is preserved as backwards-compat kwarg; converted to
      hours internally.

    Callers should look up the relevant set via the dict and pass it to
    ``evaluate_fit(..., recent_topics=...)``. A target's scope is computed
    via :func:`_log_entry_scope_key` on the target dict.

    Returns dict mapping scope_key → set of canonical signal fingerprints
    drafted on for that scope within the window.
    """
    p = log_path or _social_log_path()
    if not p.exists():
        return {}
    # Backwards compatibility: if cooldown_days passed, convert to hours.
    if cooldown_days is not None:
        cooldown_hours = float(cooldown_days) * 24.0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    seen: Dict[str, Set[str]] = {}
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
                ts = _parse_iso(entry.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                scope_key = _log_entry_scope_key(entry)
                # Use the post body + thread title (when present) as the
                # topic surface. Reddit entries log the thread title
                # separately; X entries don't have one but the post body
                # carries the @-prefixed reply context.
                blob = " ".join(
                    [
                        entry.get("text") or "",
                        entry.get("thread_title") or "",
                    ]
                )
                fps = _extract_topic_fingerprint(blob)
                if not fps:
                    continue
                seen.setdefault(scope_key, set()).update(fps)
    except OSError as exc:
        logger.warning("fit_floor: failed to read %s: %s", p, exc)
    return seen


def topics_for_scope(
    recent_topics_dict: Dict[str, Set[str]],
    scope_key: str,
) -> Set[str]:
    """Convenience helper: look up the topic set for a given scope.

    Returns empty set if the scope has no recent activity in the window.
    Callers should pass the result as ``evaluate_fit(..., recent_topics=...)``.
    """
    return recent_topics_dict.get(scope_key, set())


def target_scope_key(target: dict) -> str:
    """Compute the scope key for a target dict (for cooldown lookup).

    Mirror of :func:`_log_entry_scope_key` for the inbound target side.
    Targets may carry `platform` plus (for Reddit) `subreddit` extracted
    from the thread URL.
    """
    platform = (target.get("platform") or "").lower()
    if not platform:
        return "unknown"
    if platform == "reddit":
        sub = (target.get("subreddit") or "").lower()
        if not sub:
            # Try to extract from canonical_url / thread_url
            url = target.get("canonical_url") or target.get("thread_url") or ""
            import re as _re
            m = _re.search(r"/r/([A-Za-z0-9_]+)", url)
            if m:
                sub = m.group(1).lower()
        if sub:
            sub = sub.lstrip("r/")
            return f"reddit:r/{sub}"
        return "reddit"
    if platform == "twitter":
        return "x"
    return platform


# ── Fit-floor decision ──────────────────────────────────────────────


def evaluate_fit(
    text: str,
    engagement_score: float = 0.0,
    high_engagement_floor: float = DEFAULT_HIGH_ENGAGEMENT_FLOOR,
    recent_topics: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Decide whether a thread/target passes the Delimit-fit floor.

    Args:
        text: full thread / target text. Empty string returns a hard
            rejection (cannot evaluate).
        engagement_score: pre-computed engagement score (e.g. from
            ``x_ranker.score_target``). Used for the opportunity-cost
            carve-out: very high scores pass the floor even without a
            keyword match, but with ``human_only=True``.
        high_engagement_floor: score threshold for the carve-out.
        recent_topics: set of topic fingerprints we've drafted on inside
            the cooldown window. When the candidate's matched signals
            intersect this set, the candidate is rejected with
            ``reason="topic_cooldown"``. ``None`` skips the cooldown
            check (callers that don't want it just pass ``set()`` or
            ``None``).

    Returns:
        Dict with:
            - ``passed`` (bool)
            - ``reason`` (str): "delimit_domain" | "orbit_with_context"
              | "high_engagement_human_only" | "no_delimit_fit"
              | "topic_cooldown" | "empty_text"
            - ``human_only`` (bool): True only for the high-engagement
              carve-out — caller should NOT auto-draft.
            - ``matched_signals`` (list[str]): which canonical signal terms
              fired, for audit / replay.
            - ``topic_fingerprint`` (list[str]): for downstream cooldown
              tracking.
    """
    if not text or not text.strip():
        return {
            "passed": False,
            "reason": "empty_text",
            "human_only": False,
            "matched_signals": [],
            "topic_fingerprint": [],
        }

    domain_matches = sorted({m.group(0).lower() for m in _DELIMIT_DOMAIN_RE.finditer(text)})
    orbit_matches = sorted({m.group(0).lower() for m in _ORBIT_RE.finditer(text)})
    has_tech_context = bool(_TECH_CONTEXT_RE.search(text))

    matched_signals = sorted(set(domain_matches) | set(orbit_matches))
    topic_fp = matched_signals[:]

    # Cooldown check — runs FIRST among the pass paths so a thread that
    # would have qualified by Delimit-domain signal is still rejected if
    # the topic is already covered. (Pass-path checks below only run if
    # the cooldown didn't kill the candidate.)
    if recent_topics:
        overlap = set(topic_fp) & recent_topics
        if overlap:
            return {
                "passed": False,
                "reason": "topic_cooldown",
                "human_only": False,
                "matched_signals": matched_signals,
                "topic_fingerprint": topic_fp,
                "cooldown_overlap": sorted(overlap),
            }

    # Pass path 1: direct Delimit-domain hit.
    if domain_matches:
        return {
            "passed": True,
            "reason": "delimit_domain",
            "human_only": False,
            "matched_signals": matched_signals,
            "topic_fingerprint": topic_fp,
        }

    # Pass path 2: orbit hit + technical-context word.
    if orbit_matches and has_tech_context:
        return {
            "passed": True,
            "reason": "orbit_with_context",
            "human_only": False,
            "matched_signals": matched_signals,
            "topic_fingerprint": topic_fp,
        }

    # Pass path 3: high-engagement opportunity-cost carve-out. Pass
    # through but flag human_only so the caller doesn't auto-draft.
    if engagement_score >= high_engagement_floor:
        return {
            "passed": True,
            "reason": "high_engagement_human_only",
            "human_only": True,
            "matched_signals": matched_signals,
            "topic_fingerprint": topic_fp,
        }

    # Default: reject.
    return {
        "passed": False,
        "reason": "no_delimit_fit",
        "human_only": False,
        "matched_signals": matched_signals,
        "topic_fingerprint": topic_fp,
    }


# ── Audit logging helpers ──────────────────────────────────────────

def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    """Append a single JSON line to ``path``. Tolerant of write errors —
    logging must never crash the caller's pipeline.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort logging
        logger.warning("fit_floor: failed to write %s: %s", path, exc)


__all__ = [
    "DEFAULT_COOLDOWN_DAYS",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_HIGH_ENGAGEMENT_FLOOR",
    "SOCIAL_LOG",
    "DELIMIT_DOMAIN_SIGNALS",
    "ORBIT_SIGNALS",
    "TECH_CONTEXT_SIGNALS",
    "evaluate_fit",
    "append_jsonl",
    "_extract_topic_fingerprint",
    "_recent_topic_fingerprints",
    "_log_entry_scope_key",
    "target_scope_key",
    "topics_for_scope",
]
