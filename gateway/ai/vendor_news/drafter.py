"""Vendor-news riff drafter (LED-1250).

Takes a triggered tweet from ``ai.vendor_news.sensor.scan_vendor_news``
and generates a brand-voice Delimit-POV riff that rides the news cycle
without @-mentioning the vendor (founder convention).

Decision flow:

    triggered_tweet
        ↓ paraphrase prompt
    generate_tailored_draft (LED-791 brand voice)
        ↓
    capability_validator.validate_draft  (LED-1240 — canonical phrase + URL anchor)
        ↓ ok
    fit_floor.evaluate_fit  (LED-1240b — selectivity bar)
        ↓ pass
    insert at top of ~/.delimit/tweet_queue.json (P0, vendor_news_riff)

Both gates are HARD. A riff that fails either lands in
``~/.delimit/vendor_news_rejected.jsonl`` and the function returns
``decision="reject"``. No bypass — that's the contract from the
directive.

Per-vendor rate cap: at most 1 riff per vendor per 24h, computed by
walking the queue + the rejected log + the existing social_log.jsonl
posts. The cap is enforced BEFORE prompting the LLM so we don't burn
tokens on a draft we'll never queue.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── paths ─────────────────────────────────────────────────────────────

TWEET_QUEUE_PATH = Path.home() / ".delimit" / "tweet_queue.json"
REJECTED_LOG_PATH = Path.home() / ".delimit" / "vendor_news_rejected.jsonl"
RIFF_HISTORY_PATH = Path.home() / ".delimit" / "vendor_news_history.jsonl"

DEFAULT_RATE_CAP_HOURS = 24
MAX_TWEET_LEN = 280


# ── helpers ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_queue(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = Path(path) if path else TWEET_QUEUE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, ValueError):
        return []


def _save_queue(queue: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    p = Path(path) if path else TWEET_QUEUE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(queue, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort
        logger.warning("vendor_news: jsonl write failed for %s: %s", path, exc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ── per-vendor rate cap ──────────────────────────────────────────────


def _recent_riffs_for_vendor(
    vendor: str,
    since: datetime,
    queue_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return riffs for ``vendor`` that landed in the queue OR the
    history log inside the cap window.

    Walks two sources because:
      * the queue carries pending + recently-posted entries, and
      * the history log is the audit trail when the queue rotates them
        out (queue is mutated by the cron after post).

    Vendor matching is case-insensitive on the ``riff_vendor`` field.
    """
    vnorm = (vendor or "").strip().lower()
    if not vnorm:
        return []

    out: List[Dict[str, Any]] = []
    queue = _load_queue(queue_path)
    for entry in queue:
        if (entry.get("riff_vendor") or "").lower() != vnorm:
            continue
        added = _parse_iso(entry.get("added_at"))
        if added is None or added >= since:
            out.append(entry)

    hp = Path(history_path) if history_path else RIFF_HISTORY_PATH
    if hp.exists():
        try:
            with open(hp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if (entry.get("vendor") or "").lower() != vnorm:
                        continue
                    ts = _parse_iso(entry.get("ts"))
                    if ts is None or ts >= since:
                        out.append(entry)
        except OSError:
            pass
    return out


def _rate_capped(
    vendor: str,
    cap_hours: int = DEFAULT_RATE_CAP_HOURS,
    queue_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    cur = now or _now()
    cutoff = cur - timedelta(hours=int(cap_hours))
    return bool(_recent_riffs_for_vendor(vendor, cutoff, queue_path, history_path))


# ── prompt construction ─────────────────────────────────────────────


_VENDOR_AT_RE_TEMPLATE = r"@%s\b"


def _strip_at_mentions(text: str, no_at_handles: List[str]) -> str:
    """Defensive: even if the LLM tries to @-tag a watched handle, strip
    it. Keeps the raw text otherwise — only collapses the leading ``@``.

    Example: "Anthropic's @AnthropicAI shipped …" → "Anthropic's
    AnthropicAI shipped …". The handle word remains so the sentence
    still parses, but the algorithm-targeting @-tag is gone.
    """
    out = text
    for h in no_at_handles:
        if not h:
            continue
        pat = re.compile(_VENDOR_AT_RE_TEMPLATE % re.escape(h), re.IGNORECASE)
        out = pat.sub(h, out)
    return out


def _build_riff_prompt(
    triggered: Dict[str, Any],
    no_at_mention: bool = True,
) -> str:
    """Construct the input prompt for ``generate_tailored_draft``.

    The function returns *prompt text* — not a fully composed system
    prompt. ``generate_tailored_draft`` already handles tone / brand
    voice / style anchors / the LED-1240 ground-truth feed; we just
    need to feed it the news context + Delimit-POV instructions so it
    has something to riff on.
    """
    vendor = triggered.get("vendor") or ""
    products = ", ".join(triggered.get("products") or []) or "(none listed)"
    src_url = triggered.get("url") or ""
    raw_text = (triggered.get("text") or "").strip()
    metrics = triggered.get("metrics") or {}

    lines = [
        "VENDOR NEWS RIFF — write a brand-voice Delimit POV that rides this news cycle.",
        "",
        f"Vendor: {vendor}",
        f"Products: {products}",
        f"Source URL: {src_url}",
        f"Source metrics: {metrics.get('favorite_count', 0)} likes, "
        f"{metrics.get('retweet_count', 0)} retweets, "
        f"{metrics.get('quote_count', 0)} quotes",
        "",
        "What the vendor said (paraphrase only, do NOT quote verbatim):",
        f"  {raw_text[:500]}",
        "",
        "Write ONE original tweet (not a reply, not a quote tweet) that:",
        f"  * names the vendor by bare name only ({vendor}). "
        f"NEVER use the @ tag."
        if no_at_mention
        else f"  * names the vendor ({vendor}).",
        "  * paraphrases the news in your own words (one short clause).",
        "  * ties to a Delimit canonical claim — merge gate for AI-written code, "
        "signed replayable attestation, or cross-vendor governance.",
        "  * includes a delimit.ai URL anchor (delimit.ai/methodology, "
        "delimit.ai/reports, delimit.ai/att, or delimit.ai itself).",
        "  * stays under 280 characters.",
        "  * uses brand voice (no first person, no 'I', no 'we'). "
        "Confident technical, NOT salesy.",
        "  * does NOT use em dashes or en dashes.",
        "",
        "Output ONLY the tweet text. No preamble, no labels, no quotes around it.",
    ]
    return "\n".join(lines)


# ── queue insertion ─────────────────────────────────────────────────


def _insert_p0_at_top(
    text: str,
    *,
    triggered: Dict[str, Any],
    queue_path: Optional[Path] = None,
    image_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Insert a vendor_news_riff entry at the top of the tweet queue.

    Returns the entry that was inserted.
    """
    queue = _load_queue(queue_path)
    cur = now or _now()
    entry: Dict[str, Any] = {
        "text": text,
        "added_at": cur.isoformat(),
        "posted": False,
        "posted_at": None,
        "tweet_id": None,
        "priority": "P0",
        "category": "vendor_news_riff",
        "riff_source": triggered.get("id"),
        "riff_source_url": triggered.get("url"),
        "riff_vendor": triggered.get("vendor"),
        "riff_products": list(triggered.get("products") or []),
    }
    if image_url:
        entry["image_url"] = image_url
    queue.insert(0, entry)
    _save_queue(queue, queue_path)
    return entry


# ── main entry ───────────────────────────────────────────────────────


def draft_vendor_riff(
    triggered_tweet: Dict[str, Any],
    *,
    no_at_mention: bool = True,
    no_at_handles: Optional[List[str]] = None,
    rate_cap_hours: int = DEFAULT_RATE_CAP_HOURS,
    queue_path: Optional[Path] = None,
    rejected_log_path: Optional[Path] = None,
    history_log_path: Optional[Path] = None,
    generator=None,
    capability_validator=None,
    fit_floor=None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Generate a brand-voice Delimit-POV riff on a triggered vendor post.

    Args:
        triggered_tweet: A dict from ``scan_vendor_news()['triggered']``.
        no_at_mention: When True (default), strip @-tags of watched
            handles from the generated text before validation.
        no_at_handles: Optional list of handles to strip @-tags for. If
            omitted, falls back to the triggered tweet's ``author``.
        rate_cap_hours: Per-vendor cooldown window in hours.
        queue_path / rejected_log_path / history_log_path: test hooks.
        generator: Callable(prompt:str, platform:str, venture:str,
            account:str) -> str. Defaults to
            ``ai.social.generate_tailored_draft``. Test hook.
        capability_validator: Callable(text:str, platform:str) -> dict
            with at least an ``ok`` key. Defaults to
            ``ai.social_capability.capability_validator.validate_draft``.
            Test hook.
        fit_floor: Callable(text:str) -> dict with at least ``passed``.
            Defaults to ``ai.social_capability.fit_floor.evaluate_fit``.
            Test hook.
        now: Override "current time" (test hook).

    Returns:
        Dict with:
            decision: "queue" | "reject"
            text: generated draft text (may be empty on early reject)
            reason: short reason tag if decision == "reject"
            queue_entry: the inserted queue entry on decision == "queue"
            validator_result: capability_validator return dict
            fit_result: fit_floor return dict
    """
    triggered_tweet = triggered_tweet or {}
    cur = now or _now()
    vendor = triggered_tweet.get("vendor") or ""

    result: Dict[str, Any] = {
        "decision": "reject",
        "text": "",
        "reason": "",
        "queue_entry": None,
        "validator_result": None,
        "fit_result": None,
    }

    # 1) Per-vendor rate cap. Check BEFORE prompting the LLM.
    if vendor and _rate_capped(
        vendor,
        cap_hours=rate_cap_hours,
        queue_path=queue_path,
        history_path=history_log_path,
        now=cur,
    ):
        result["reason"] = "rate_capped"
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "reason": "rate_capped",
            },
        )
        return result

    # 2) Resolve dependency callables.
    if generator is None:
        try:
            from ai.social import generate_tailored_draft as _generator
            generator = _generator
        except Exception as exc:
            result["reason"] = f"generator_unavailable:{exc}"
            return result

    if capability_validator is None:
        try:
            from ai.social_capability.capability_validator import (
                validate_draft as _validate,
            )
            capability_validator = _validate
        except Exception as exc:
            result["reason"] = f"validator_unavailable:{exc}"
            return result

    if fit_floor is None:
        try:
            from ai.social_capability.fit_floor import evaluate_fit as _evaluate
            fit_floor = _evaluate
        except Exception as exc:
            result["reason"] = f"fit_floor_unavailable:{exc}"
            return result

    # 2b) Source-post pre-filter: check the SOURCE tweet text against the
    # fit_floor BEFORE invoking the LLM. If the vendor news is off-topic
    # for Delimit (e.g., image generation, exec drama, marketing fluff),
    # there is no authentic riff to write — abstain without burning tokens
    # AND without ticking the per-vendor 24h rate cap. Founder direction
    # 2026-05-07 after live xAI image-gen post correctly fell through to
    # fit_floor at draft time but wasted an LLM call to get there.
    source_text = triggered_tweet.get("text") or ""
    try:
        source_fit = fit_floor(source_text)
    except Exception as exc:
        # Don't block the pipeline on a fit_floor bug; log and continue.
        source_fit = {"passed": True, "reason": f"source_fit_error:{exc}"}
    if not source_fit.get("passed"):
        result["reason"] = "source_off_topic"
        result["fit_result"] = source_fit
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "source_text": source_text[:200],
                "reason": "source_off_topic",
                "source_fit": source_fit,
            },
        )
        return result

    # 3) Build the prompt + generate.
    prompt = _build_riff_prompt(triggered_tweet, no_at_mention=no_at_mention)
    try:
        text = generator(
            prompt,
            "twitter",
            "delimit",
            "delimit_ai",
        ) or ""
    except TypeError:
        # Older signature without account kwarg — rare; fall back.
        try:
            text = generator(prompt, "twitter", "delimit") or ""
        except Exception as exc:
            result["reason"] = f"generator_error:{exc}"
            return result
    except Exception as exc:
        result["reason"] = f"generator_error:{exc}"
        return result

    text = (text or "").strip()
    if not text:
        result["reason"] = "empty_draft"
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "reason": "empty_draft",
            },
        )
        return result

    # 4) Strip @-mentions defensively. The drafter prompt forbids them
    #    but LLMs drift; this is a belt-and-suspenders check before the
    #    capability validator sees the text.
    if no_at_mention:
        handles = list(no_at_handles or [])
        if not handles and triggered_tweet.get("author"):
            handles = [triggered_tweet["author"]]
        text = _strip_at_mentions(text, handles)

    # 5) Length cap (defensive — generator should already respect 280).
    if len(text) > MAX_TWEET_LEN:
        text = text[:MAX_TWEET_LEN].rstrip()

    result["text"] = text

    # 6) Capability validator gate (LED-1240).
    try:
        validator_result = capability_validator(text, platform="twitter")
    except TypeError:
        validator_result = capability_validator(text)
    result["validator_result"] = validator_result

    if not (validator_result or {}).get("ok"):
        result["reason"] = "validator_failed"
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "text": text,
                "reason": "validator_failed",
                "validator": {
                    "errors": validator_result.get("errors") if isinstance(validator_result, dict) else [],
                    "warnings": validator_result.get("warnings") if isinstance(validator_result, dict) else [],
                },
            },
        )
        return result

    # 7) Fit-floor gate (LED-1240b).
    try:
        fit_result = fit_floor(text)
    except Exception as exc:
        fit_result = {"passed": False, "reason": f"fit_floor_error:{exc}"}
    result["fit_result"] = fit_result

    if not (fit_result or {}).get("passed"):
        result["reason"] = "fit_floor_failed"
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "text": text,
                "reason": "fit_floor_failed",
                "fit": {
                    "reason": fit_result.get("reason") if isinstance(fit_result, dict) else "",
                    "matched_signals": fit_result.get("matched_signals") if isinstance(fit_result, dict) else [],
                },
            },
        )
        return result

    # human_only carve-out: do NOT auto-queue. Log and return reject so
    # the orchestrator can surface for review later.
    if (fit_result or {}).get("human_only"):
        result["reason"] = "fit_floor_human_only"
        _append_jsonl(
            Path(rejected_log_path) if rejected_log_path else REJECTED_LOG_PATH,
            {
                "ts": cur.isoformat(),
                "vendor": vendor,
                "source_id": triggered_tweet.get("id"),
                "text": text,
                "reason": "fit_floor_human_only",
            },
        )
        return result

    # 8) Queue insert (P0, vendor_news_riff).
    entry = _insert_p0_at_top(
        text,
        triggered=triggered_tweet,
        queue_path=queue_path,
        now=cur,
    )

    # Append to history so the rate cap survives queue rotation.
    _append_jsonl(
        Path(history_log_path) if history_log_path else RIFF_HISTORY_PATH,
        {
            "ts": cur.isoformat(),
            "vendor": vendor,
            "source_id": triggered_tweet.get("id"),
            "source_url": triggered_tweet.get("url"),
            "text": text,
        },
    )

    result["decision"] = "queue"
    result["queue_entry"] = entry
    result["reason"] = ""
    return result


__all__ = [
    "DEFAULT_RATE_CAP_HOURS",
    "MAX_TWEET_LEN",
    "REJECTED_LOG_PATH",
    "RIFF_HISTORY_PATH",
    "TWEET_QUEUE_PATH",
    "draft_vendor_riff",
]
