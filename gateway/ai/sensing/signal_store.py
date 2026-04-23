"""Signal store (LED-877).

Physically separated from the ledger. Daily shards, append-only. Reuses
~/.delimit/intel/ as the parent directory so intel_* tooling can already
query it via intel_dataset_list.

Consumers: delimit sense CLI, delimit_signals_query MCP tool (future).
NOT a consumer: build_loop, agent_dispatch, ledger_manager.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ai.sensing.schema import (
    Signal,
    ValidationError,
    fingerprint_of,
    normalize_url,
    validate_and_normalize,
)


class StorageError(RuntimeError):
    """Raised when signal persistence fails."""


INTEL_DIR = Path.home() / ".delimit" / "intel"
SIGNALS_DIR = INTEL_DIR / "signals"
ARCHIVE_DIR = SIGNALS_DIR / "archive"
DEDUP_INDEX_PATH = SIGNALS_DIR / "_dedup_index.json"

HOT_WINDOW_DAYS = 7
WARM_WINDOW_DAYS = 30
MAX_SIGNALS_PER_AUTHOR_PER_DAY = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_shard_path(when: Optional[datetime] = None) -> Path:
    when = when or _now()
    return SIGNALS_DIR / f"{when.date().isoformat()}.jsonl"


def _ensure_dirs() -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _load_dedup_index() -> Dict[str, Dict[str, Any]]:
    if not DEDUP_INDEX_PATH.exists():
        return {}
    try:
        return json.loads(DEDUP_INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_dedup_index(index: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dirs()
    tmp = DEDUP_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index))
    tmp.replace(DEDUP_INDEX_PATH)


def dedup_check(fingerprint: str, window_days: int = WARM_WINDOW_DAYS) -> bool:
    """Return True if a signal with this fingerprint was ingested within window_days."""
    if not fingerprint:
        return False
    index = _load_dedup_index()
    entry = index.get(fingerprint)
    if not entry:
        return False
    try:
        ingested = datetime.fromisoformat(entry.get("ingested_at", ""))
    except ValueError:
        return False
    cutoff = _now() - timedelta(days=window_days)
    return ingested >= cutoff


def _author_count_today(author: str) -> int:
    if not author:
        return 0
    path = _today_shard_path()
    if not path.exists():
        return 0
    count = 0
    needle = author.lower()
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("author") or "").lower() == needle:
                count += 1
    except OSError:
        return 0
    return count


def ingest(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Ingest a raw target dict as a validated Signal.

    Raises ValidationError on schema violation (caller decides whether to
    log-and-skip or propagate). Returns the stored signal with assigned id
    and ingested_at, or a dedup/rate-limit notice.
    """
    signal = validate_and_normalize(raw)

    if dedup_check(signal.fingerprint):
        index = _load_dedup_index()
        entry = index.get(signal.fingerprint, {})
        entry["hit_count"] = int(entry.get("hit_count", 1)) + 1
        entry["last_seen_at"] = _now().isoformat()
        index[signal.fingerprint] = entry
        _save_dedup_index(index)
        return {
            "status": "deduped",
            "fingerprint": signal.fingerprint,
            "hit_count": entry["hit_count"],
        }

    if _author_count_today(signal.author) >= MAX_SIGNALS_PER_AUTHOR_PER_DAY:
        return {
            "status": "rate_limited",
            "author": signal.author,
            "limit": MAX_SIGNALS_PER_AUTHOR_PER_DAY,
        }

    _ensure_dirs()
    now = _now()
    signal.ingested_at = now.isoformat()
    signal.id = f"SIG-{uuid.uuid4().hex[:10].upper()}"

    shard = _today_shard_path(now)
    try:
        with shard.open("a") as f:
            f.write(json.dumps(signal.to_dict()) + "\n")
    except OSError as exc:
        raise StorageError(f"failed to write signal shard {shard}: {exc}") from exc

    index = _load_dedup_index()
    index[signal.fingerprint] = {
        "id": signal.id,
        "ingested_at": signal.ingested_at,
        "hit_count": 1,
        "shard": shard.name,
    }
    _save_dedup_index(index)

    return {"status": "ingested", "signal": signal.to_dict(), "shard": shard.name}


def _iter_shards(since_days: int = HOT_WINDOW_DAYS) -> Iterable[Path]:
    if not SIGNALS_DIR.exists():
        return []
    cutoff = (_now() - timedelta(days=since_days)).date()
    paths = []
    for path in SIGNALS_DIR.glob("*.jsonl"):
        if path.name.startswith("_"):
            continue
        try:
            shard_date = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        if shard_date >= cutoff:
            paths.append(path)
    return sorted(paths, reverse=True)


def query(
    since_days: int = 1,
    platform: str = "",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return signals from the hot window, newest first.

    since_days=1 returns the last 24h of signals (the default `delimit sense`
    view). platform filters to a specific source; empty = all.
    """
    rows: List[Dict[str, Any]] = []
    want_platform = (platform or "").strip().lower()
    for shard in _iter_shards(since_days):
        try:
            for line in shard.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if want_platform and (row.get("platform") or "").lower() != want_platform:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    return rows
        except OSError:
            continue
    return rows


def age_out_to_warm(days: int = HOT_WINDOW_DAYS) -> int:
    """No-op placeholder: hot/warm separation is a query boundary, not a move.

    We keep all shards in SIGNALS_DIR and rely on query()'s since_days filter
    to enforce the hot window. Returns the count of shards older than `days`
    for reporting.
    """
    if not SIGNALS_DIR.exists():
        return 0
    cutoff = (_now() - timedelta(days=days)).date()
    old = 0
    for path in SIGNALS_DIR.glob("*.jsonl"):
        if path.name.startswith("_"):
            continue
        try:
            shard_date = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        if shard_date < cutoff:
            old += 1
    return old


def freeze_cold(month: str) -> str:
    """Move all shards whose date starts with `month` (YYYY-MM) into ARCHIVE_DIR/{month}.jsonl.

    Returns the archive path. Safe to run repeatedly; reruns append.
    """
    if not month or len(month) != 7 or month[4] != "-":
        raise ValidationError(f"month must be YYYY-MM, got {month!r}")
    _ensure_dirs()
    archive_path = ARCHIVE_DIR / f"{month}.jsonl"
    moved = 0
    with archive_path.open("a") as out:
        for path in sorted(SIGNALS_DIR.glob(f"{month}-*.jsonl")):
            try:
                out.write(path.read_text())
            except OSError:
                continue
            try:
                path.unlink()
                moved += 1
            except OSError:
                pass
    return str(archive_path)


def promote_to_ledger(
    signal_id: str,
    ledger: str = "ops",
    priority: str = "P2",
    extra_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Explicit founder-initiated promotion of a signal to a ledger item.

    This is the ONLY path from intel → ledger. Writes via ledger_manager.add_item
    with promoted_by set so the guard accepts it.
    """
    signal = _find_signal(signal_id)
    if not signal:
        raise ValidationError(f"signal {signal_id} not found in hot shards")

    from ai.ledger_manager import add_item

    title = f"[{(signal.get('platform') or 'signal').upper()}] Promoted: {signal.get('author') or signal.get('canonical_url')}"
    description = (
        f"Promoted from intel store (signal {signal_id}).\n"
        f"URL: {signal.get('canonical_url', '')}\n"
        f"Author: {signal.get('author', '')}\n"
        f"Snippet: {(signal.get('content_snippet') or '')[:400]}\n"
        f"Posted: {signal.get('posted_at', '')}\n"
        f"Fingerprint: {signal.get('fingerprint', '')}"
    )
    tags = ["promoted-signal", signal.get("platform", "")]
    if extra_tags:
        tags.extend(extra_tags)

    # Guard checks source=='promoted_signal' + promoted_by set, so bypass the
    # social_scan rejection.
    os.environ.setdefault("_DELIMIT_SIGNAL_PROMOTED_BY", "founder")
    try:
        result = add_item(
            title=title,
            ledger=ledger,
            type="task",
            priority=priority,
            description=description,
            source=f"promoted_signal:{signal_id}",
            tags=tags,
            context=f"Promoted from signal {signal_id} for strategic action.",
        )
    finally:
        os.environ.pop("_DELIMIT_SIGNAL_PROMOTED_BY", None)
    return result


def _find_signal(signal_id: str) -> Optional[Dict[str, Any]]:
    if not signal_id:
        return None
    for shard in _iter_shards(since_days=WARM_WINDOW_DAYS):
        try:
            for line in shard.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("id") == signal_id:
                    return row
        except OSError:
            continue
    return None


def digest(since_days: int = HOT_WINDOW_DAYS, top_n: int = 20) -> Dict[str, Any]:
    """Cluster recent signals by platform + top authors + theme counters."""
    rows = query(since_days=since_days, limit=1000)
    by_platform: Counter[str] = Counter()
    by_author: Counter[str] = Counter()
    by_theme: Counter[str] = Counter()
    for row in rows:
        by_platform[row.get("platform", "?")] += 1
        by_author[row.get("author", "?")] += 1
        for theme in row.get("themes") or []:
            by_theme[theme] += 1
    return {
        "window_days": since_days,
        "total_signals": len(rows),
        "top_platforms": by_platform.most_common(10),
        "top_authors": by_author.most_common(top_n),
        "top_themes": by_theme.most_common(top_n),
        "sample": rows[:5],
    }
