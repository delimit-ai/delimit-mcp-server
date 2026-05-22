"""LED-1264 scan-bridge — promotion engine.

Reads ``~/.delimit/social_targets.jsonl`` (the existing
``delimit_social_target`` output), filters to the tight panel-locked
gate, runs dedup against the strategy ledger, and promotes survivors
via direct in-process ``ledger_manager.add_item`` calls.

State / cursor:
    ``~/.delimit/scan_bridge_cursor.json`` records the most-recent
    ``first_seen`` value we've already processed. Subsequent runs only
    consider lines newer than that. Idempotent — re-running the cron
    on the same JSONL is a no-op.

Promotions log:
    ``~/.delimit/scan_bridge_promotions.jsonl`` records every successful
    promotion (item_id, signal_fingerprint, ts) so the daily digest can
    assemble the last-24h batch without re-walking the ledger.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ai.scan_bridge.dedup import (
    _candidate_strategy_items,
    extract_topic_fingerprint,
    is_duplicate,
)

logger = logging.getLogger("delimit.ai.scan_bridge.bridge")

TARGETS_FILE = Path.home() / ".delimit" / "social_targets.jsonl"
CURSOR_FILE = Path.home() / ".delimit" / "scan_bridge_cursor.json"
PROMOTIONS_LOG = Path.home() / ".delimit" / "scan_bridge_promotions.jsonl"


def _confidence_floor() -> float:
    """Resolve the active confidence floor (env-overridable per directive)."""
    raw = os.environ.get("DELIMIT_SCAN_PROMO_CONFIDENCE", "")
    if not raw:
        return 0.85
    try:
        v = float(raw)
        if 0.0 <= v <= 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return 0.85


# ── Cursor I/O ────────────────────────────────────────────────────────


def _load_cursor() -> Optional[str]:
    """Return the most-recent ``first_seen`` we've already processed."""
    if not CURSOR_FILE.exists():
        return None
    try:
        data = json.loads(CURSOR_FILE.read_text())
        v = data.get("last_seen_at")
        return str(v) if v else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_cursor(last_seen_at: str) -> None:
    try:
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(json.dumps({"last_seen_at": last_seen_at}))
    except OSError:  # pragma: no cover — best-effort
        logger.warning("scan_bridge: failed to persist cursor")


def _log_promotion(record: Dict[str, Any]) -> None:
    try:
        PROMOTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PROMOTIONS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:  # pragma: no cover — best-effort
        pass


# ── Filtering ─────────────────────────────────────────────────────────


@dataclass
class _FilterStats:
    considered: int = 0
    rejected_classification: int = 0
    rejected_confidence: int = 0
    rejected_dedup: int = 0
    promoted: int = 0


def _passes_strict_gate(
    signal: Dict[str, Any],
    *,
    confidence_floor: float,
    stats: _FilterStats,
) -> Tuple[bool, str]:
    """Return ``(passes, reason)``. ``reason`` is "" on pass."""
    classification = (signal.get("classification") or "").strip().lower()
    if classification != "strategic":
        stats.rejected_classification += 1
        return False, f"classification={classification or 'missing'}"
    try:
        confidence = float(signal.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < confidence_floor:
        stats.rejected_confidence += 1
        return False, f"confidence={confidence:.2f}<{confidence_floor:.2f}"
    return True, ""


# ── Promotion path ────────────────────────────────────────────────────


def _build_title(signal: Dict[str, Any]) -> str:
    snippet = (signal.get("content_snippet") or "").strip()
    # If the snippet starts with a "[TAG] head" prefix the tag + head
    # makes the most readable title. Otherwise fall back to the first
    # 80 chars of the snippet.
    if snippet.startswith("["):
        head = snippet.split("\n", 1)[0]
        if len(head) > 120:
            head = head[:117] + "..."
        return f"STRATEGIC: {head}"
    if len(snippet) > 100:
        snippet = snippet[:97] + "..."
    return f"STRATEGIC: {snippet}" if snippet else "STRATEGIC: (no snippet)"


def _build_item(signal: Dict[str, Any]) -> Dict[str, Any]:
    platform = signal.get("platform") or ""
    canonical_url = signal.get("canonical_url") or ""
    snippet = (signal.get("content_snippet") or "")[:280]
    confidence = float(signal.get("confidence") or 0.0)
    first_seen = signal.get("first_seen") or ""
    source_id = signal.get("source_id") or signal.get("fingerprint") or ""

    fingerprint_set = sorted(extract_topic_fingerprint(signal))

    description = (
        f"Auto-promoted from {platform} signal at {confidence:.2f}: "
        f"{snippet}\n\nURL: {canonical_url or '(none)'}"
    )
    context_text = (
        f"Captured by delimit_social_target on {first_seen}. "
        "Panel-approved auto-promote (LED-1264) per deliberation 2026-05-07. "
        "Founder reviews via daily digest."
    )

    return {
        "title": _build_title(signal),
        "ledger": "strategy",
        "type": "strategy",
        "priority": "P2",
        "description": description,
        "context": context_text,
        "tags": ["auto_promoted", "scan_bridge", platform] if platform else ["auto_promoted", "scan_bridge"],
        "source": "scan_bridge_auto",
        "metadata_signal_ref": {
            "platform": platform,
            "source_id": source_id,
            "fingerprint": fingerprint_set,
            "first_seen": first_seen,
            "confidence": confidence,
            "canonical_url": canonical_url,
        },
    }


@contextmanager
def _signal_promote_bypass():
    """Set ``_DELIMIT_SIGNAL_PROMOTED_BY`` so the LED-877 guard treats
    this as the explicit promote path. Defensive against future source
    name changes — guard currently allows ``scan_bridge_auto`` since it
    doesn't start with the sensed prefixes, but this future-proofs.
    """
    key = "_DELIMIT_SIGNAL_PROMOTED_BY"
    prev = os.environ.get(key)
    os.environ[key] = "scan_bridge:LED-1264"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def _add_to_strategy_ledger(item: Dict[str, Any]) -> Dict[str, Any]:
    """Direct in-process call to ``ledger_manager.add_item``.

    The ledger module currently doesn't accept a ``metadata`` kwarg, so
    we splice signal_ref into the description as a fenced JSON block AND
    embed the fingerprint tokens into the tags list. Future ledger
    schema enhancements that add a metadata column should swap this in
    without changing the caller surface.
    """
    from ai.ledger_manager import add_item

    signal_ref = item.pop("metadata_signal_ref", {})
    fp_tokens = signal_ref.get("fingerprint") or []
    fingerprint_tags = [f"fp:{t}" for t in fp_tokens][:8]  # cap to keep tag list sane

    # Append fenced JSON to description so tools that read raw description
    # can recover the signal_ref structurally; the dedup module already
    # falls back to extracting fingerprints from description text when
    # the structured field is missing, so this is also recoverable.
    sref_block = "\n\nsignal_ref:\n```json\n" + json.dumps(signal_ref, ensure_ascii=False, sort_keys=True) + "\n```"
    item["description"] = item.get("description", "") + sref_block
    item["tags"] = list(item.get("tags") or []) + fingerprint_tags

    with _signal_promote_bypass():
        return add_item(**item)


# ── Public API ────────────────────────────────────────────────────────


def _iter_signals(targets_file: Path = TARGETS_FILE) -> Iterable[Dict[str, Any]]:
    if not targets_file.exists():
        return
    try:
        with targets_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as exc:  # pragma: no cover
        logger.warning("scan_bridge: failed to read %s: %s", targets_file, exc)


def _normalize_first_seen(value: Any) -> str:
    """Return a comparable string. Empty string sorts before anything."""
    if not value:
        return ""
    return str(value)


def promote_recent_signals(
    since: Optional[datetime] = None,
    *,
    dry_run: bool = False,
    targets_file: Optional[Path] = None,
    confidence_floor: Optional[float] = None,
    candidates: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Process scanned signals from ``targets_file`` and promote
    survivors of the strict gate to the strategy ledger.

    Parameters
    ----------
    since:
        Optional cutoff. Defaults to the persisted cursor; falls back to
        24h ago when no cursor exists.
    dry_run:
        When True no ledger writes happen; the response still contains
        the would-be promotions for audit / preview.
    targets_file:
        Override the default ``social_targets.jsonl`` path (test hook).
    confidence_floor:
        Override the env-resolved floor (test hook).
    candidates:
        Override the strategy-ledger candidate list for dedup (test
        hook). When omitted we fetch live items inside ``is_duplicate``.

    Returns
    -------
    dict with keys: ``stats``, ``promoted`` (list of {item_id,
    signal_fingerprint, title, snippet}), ``cursor_advanced_to``,
    ``dry_run``.
    """
    targets_file = targets_file or TARGETS_FILE
    floor = confidence_floor if confidence_floor is not None else _confidence_floor()

    cursor_value = _load_cursor()
    if since is not None:
        # Caller-supplied since: take the LATER of since vs cursor so we
        # never reprocess a row we've already promoted.
        since_iso = since.astimezone(timezone.utc).isoformat()
        if cursor_value and cursor_value > since_iso:
            since_iso = cursor_value
    else:
        if cursor_value:
            since_iso = cursor_value
        else:
            since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    stats = _FilterStats()
    promoted: List[Dict[str, Any]] = []
    max_seen = since_iso

    # Resolve candidates ONCE per run for performance — production calls
    # don't pass it; we hand the live list to is_duplicate as a static
    # snapshot so 1000 signals don't trigger 1000 ledger walks.
    if candidates is None:
        snapshot = list(_candidate_strategy_items(window_days=60))
    else:
        snapshot = list(candidates)
    # We'll mutate snapshot during the run so an early-batch promotion
    # blocks a later-batch duplicate within the same invocation.
    live_snapshot: List[Dict[str, Any]] = list(snapshot)

    # Process newest-first within the batch so when two signals about
    # the same topic appear (e.g. oasdiff v1.15.0-beta + v1.15.2), the
    # MOST RECENT version wins. The earlier versions then dedup against
    # the newer item — which is what the founder wants in the digest.
    # We still advance the cursor to the max first_seen across the run
    # so the next call only considers genuinely-new rows.
    queued: List[Dict[str, Any]] = []
    for signal in _iter_signals(targets_file):
        first_seen = _normalize_first_seen(signal.get("first_seen"))
        if first_seen <= since_iso:
            continue
        queued.append((first_seen, signal))
    queued.sort(key=lambda pair: pair[0], reverse=True)

    for first_seen, signal in queued:
        stats.considered += 1
        if first_seen > max_seen:
            max_seen = first_seen

        passes, reason = _passes_strict_gate(
            signal, confidence_floor=floor, stats=stats
        )
        if not passes:
            continue

        match = is_duplicate(signal, window_days=60, candidates=live_snapshot)
        if match is not None:
            stats.rejected_dedup += 1
            continue

        if dry_run:
            stats.promoted += 1
            promoted.append({
                "item_id": "DRY-RUN",
                "signal_fingerprint": signal.get("fingerprint"),
                "title": _build_title(signal),
                "snippet": (signal.get("content_snippet") or "")[:200],
                "confidence": signal.get("confidence"),
                "platform": signal.get("platform"),
                "canonical_url": signal.get("canonical_url"),
                "first_seen": first_seen,
            })
            # Mirror within-batch dedup behaviour even in dry-run so the
            # preview count matches what a real run would write. Build a
            # synthetic ledger-shaped item carrying the signal's
            # fingerprint tokens.
            tokens = sorted(extract_topic_fingerprint(signal))
            now_iso = datetime.now(timezone.utc).isoformat()
            live_snapshot.append({
                "id": "DRY-RUN",
                "status": "open",
                "title": _build_title(signal),
                "description": (signal.get("content_snippet") or ""),
                "context": "",
                "tags": [],
                "created_at": now_iso,
                "updated_at": now_iso,
                "metadata": {"signal_ref": {"fingerprint": tokens}},
            })
            continue

        item = _build_item(signal)
        # Capture the signal_ref before _add_to_strategy_ledger pops it
        # off the item dict — we need it for the within-batch snapshot
        # append below so subsequent signals can dedup against this one.
        captured_signal_ref = item.get("metadata_signal_ref") or {}
        try:
            result = _add_to_strategy_ledger(item)
        except Exception as exc:
            logger.exception("scan_bridge: ledger add failed for %s", signal.get("fingerprint"))
            continue
        added = result.get("added") or {}
        item_id = added.get("id") or ""
        stats.promoted += 1
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "item_id": item_id,
            "signal_fingerprint": signal.get("fingerprint"),
            "title": item["title"],
            "platform": signal.get("platform"),
            "confidence": signal.get("confidence"),
            "canonical_url": signal.get("canonical_url"),
            "first_seen": first_seen,
        }
        _log_promotion(record)
        promoted.append({
            "item_id": item_id,
            "signal_fingerprint": signal.get("fingerprint"),
            "title": item["title"],
            "snippet": (signal.get("content_snippet") or "")[:200],
            "confidence": signal.get("confidence"),
            "platform": signal.get("platform"),
            "canonical_url": signal.get("canonical_url"),
            "first_seen": first_seen,
        })
        # Add the freshly-promoted item to the in-memory snapshot so any
        # later-but-similar signal in the same batch is correctly
        # de-duplicated.
        now_iso = datetime.now(timezone.utc).isoformat()
        live_snapshot.append({
            "id": item_id,
            "status": "open",
            "title": item["title"],
            "description": item["description"],
            "context": item.get("context", ""),
            "tags": item.get("tags") or [],
            "created_at": now_iso,
            "updated_at": now_iso,
            "metadata": {"signal_ref": captured_signal_ref},
        })

    # Advance cursor on success — only when not a dry-run.
    if not dry_run and max_seen and max_seen != since_iso:
        _save_cursor(max_seen)

    return {
        "stats": {
            "considered": stats.considered,
            "rejected_classification": stats.rejected_classification,
            "rejected_confidence": stats.rejected_confidence,
            "rejected_dedup": stats.rejected_dedup,
            "promoted": stats.promoted,
        },
        "promoted": promoted,
        "cursor_advanced_to": max_seen if (not dry_run and max_seen != since_iso) else None,
        "since": since_iso,
        "dry_run": dry_run,
        "confidence_floor": floor,
    }


def backfill_from(
    start_date: date,
    *,
    dry_run: bool = False,
    targets_file: Optional[Path] = None,
    candidates: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Walk ``targets_file`` from ``start_date`` (UTC) forward and
    promote everything that passes the strict gate.

    Idempotent — leverages the same cursor as ``promote_recent_signals``
    so re-running on the same range is a no-op (or a delta-only run if
    the file has grown).

    Per the directive: surface the candidate counts so the founder sees
    how much real signal was captured but never promoted before this
    bridge existed.
    """
    since_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    return promote_recent_signals(
        since=since_dt,
        dry_run=dry_run,
        targets_file=targets_file,
        candidates=candidates,
    )
