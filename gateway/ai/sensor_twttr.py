"""
Twttr241 FTS5 competitive intel sensor.

Queries the delimit tweet corpus (LED-766, ai/tweet_corpus.py) for competitor
and brand mentions and files ledger items for new hits. Nightly cadence.
Zero API calls -- read-only over the local SQLite corpus.

Usage (as a plain Python function):

    from ai.sensor_twttr import delimit_sensor_twttr
    result = delimit_sensor_twttr()

TODO: When coordinating the next ai/server.py release, register
`delimit_sensor_twttr` as an MCP tool so it can be invoked from ChatOps and
the nightly orchestrator.

Dedup key: f"{tweet_id}:{keyword}" persisted at
    ~/.delimit/state/sensor_twttr_seen.json

Keyword config (YAML, auto-created on first run with DEFAULT_KEYWORDS):
    ~/.delimit/config/sensor_twttr_keywords.yaml

Each keyword entry supports:
    priority: P0 | P1 | P2   (ledger priority)
    venture:  str            (venture tag for the ledger item)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = os.path.expanduser("~/.delimit/data/tweets.db")
STATE_PATH = os.path.expanduser("~/.delimit/state/sensor_twttr_seen.json")
CONFIG_PATH = os.path.expanduser("~/.delimit/config/sensor_twttr_keywords.yaml")

# How long a dedup entry lives before it is purged from the seen state.
SEEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Per-keyword FTS5 match cap.
MAX_MATCHES_PER_KEYWORD = 200

DEFAULT_KEYWORDS: Dict[str, Dict[str, str]] = {
    "delimit": {"priority": "P0", "venture": "delimit"},
    "openapi governance": {"priority": "P1", "venture": "delimit"},
    "breaking change": {"priority": "P1", "venture": "delimit"},
    "oasdiff": {"priority": "P1", "venture": "delimit"},
    "spectral api": {"priority": "P1", "venture": "delimit"},
    "optic api": {"priority": "P1", "venture": "delimit"},
    "bump.sh": {"priority": "P2", "venture": "delimit"},
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _dump_yaml(data: Dict[str, Dict[str, str]]) -> str:
    """Minimal YAML writer to avoid a hard PyYAML dep at import time."""
    lines: List[str] = [
        "# sensor_twttr keywords -- edit to tune competitive intel scanning.",
        "# Each keyword is an FTS5 MATCH expression. priority: P0|P1|P2.",
        "",
    ]
    for kw, meta in data.items():
        # Quote the key defensively -- FTS5 expressions can contain spaces/dots.
        safe_key = kw.replace('"', '\\"')
        lines.append(f'"{safe_key}":')
        lines.append(f'  priority: {meta.get("priority", "P1")}')
        lines.append(f'  venture: {meta.get("venture", "delimit")}')
    return "\n".join(lines) + "\n"


def _load_yaml(path: str) -> Dict[str, Dict[str, str]]:
    """Load keyword config. Prefer PyYAML; fall back to a tiny parser."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for k, v in loaded.items():
            if isinstance(v, dict):
                out[str(k)] = {
                    "priority": str(v.get("priority", "P1")),
                    "venture": str(v.get("venture", "delimit")),
                }
        return out
    except Exception:
        return _parse_yaml_fallback(text)


def _parse_yaml_fallback(text: str) -> Dict[str, Dict[str, str]]:
    """Tiny parser for the exact shape written by _dump_yaml."""
    out: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            key = line.rstrip(":").strip()
            if key.startswith('"') and key.endswith('"'):
                key = key[1:-1].replace('\\"', '"')
            current = key
            out[current] = {"priority": "P1", "venture": "delimit"}
        else:
            if current is None:
                continue
            kv = line.strip()
            if ":" in kv:
                k, _, v = kv.partition(":")
                out[current][k.strip()] = v.strip()
    return out


def _ensure_config() -> Dict[str, Dict[str, str]]:
    path = Path(CONFIG_PATH)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump_yaml(DEFAULT_KEYWORDS), encoding="utf-8")
        return dict(DEFAULT_KEYWORDS)
    loaded = _load_yaml(str(path))
    return loaded or dict(DEFAULT_KEYWORDS)


# ---------------------------------------------------------------------------
# Dedup state
# ---------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    path = Path(STATE_PATH)
    if not path.exists():
        return {"seen": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "seen" not in data:
            return {"seen": {}}
        if not isinstance(data["seen"], dict):
            data["seen"] = {}
        return data
    except Exception:
        return {"seen": {}}


def _save_state(state: Dict[str, Any]) -> None:
    path = Path(STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _purge_stale(state: Dict[str, Any], now: float) -> int:
    """Drop seen entries older than SEEN_TTL_SECONDS. Returns count purged."""
    seen = state.get("seen", {})
    cutoff = now - SEEN_TTL_SECONDS
    stale = [k for k, ts in seen.items() if not isinstance(ts, (int, float)) or ts < cutoff]
    for k in stale:
        del seen[k]
    return len(stale)


# ---------------------------------------------------------------------------
# Corpus query
# ---------------------------------------------------------------------------

def _open_corpus(db_path: str) -> Optional[sqlite3.Connection]:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _query_keyword(conn: sqlite3.Connection, keyword: str) -> List[sqlite3.Row]:
    try:
        cur = conn.execute(
            "SELECT t.tweet_id, t.author_handle, t.text, t.created_at, t.like_count "
            "FROM tweets t JOIN tweets_fts fts ON fts.rowid = t.rowid "
            "WHERE tweets_fts MATCH ? "
            "ORDER BY t.created_at DESC LIMIT ?",
            (keyword, MAX_MATCHES_PER_KEYWORD),
        )
        return list(cur.fetchall())
    except sqlite3.Error:
        return []


def _format_title(keyword: str, author: str, likes: int) -> str:
    return f'[SENSOR] Twttr mention: "{keyword}" by @{author} ({likes} likes)'


def _format_description(
    keyword: str,
    tweet_id: str,
    author: str,
    text: str,
    created_at: str,
    likes: int,
    venture: str,
) -> str:
    url = f"https://twitter.com/{author}/status/{tweet_id}"
    return (
        f"Twttr241 FTS5 sensor match on keyword: {keyword}\n"
        f"Venture: {venture}\n"
        f"Author: @{author}\n"
        f"Created: {created_at}\n"
        f"Likes: {likes}\n"
        f"URL: {url}\n"
        f"\n"
        f"Tweet text:\n{text}\n"
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def delimit_sensor_twttr(
    keywords_config: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run FTS5 queries over the tweet corpus and file ledger items for new matches.

    Args:
        keywords_config: Optional override path for the keyword YAML config.
        dry_run: If True, do not call ledger_manager.add_item and do not mutate
                 the seen-state file. Still loads config (auto-creating on first run)
                 and returns the would-be match counts.

    Returns:
        dict with scanned_keywords, total_matches, new_matches,
        ledger_items_created (list of ids), matches (list of detail dicts), dry_run.
    """
    config_path = keywords_config or CONFIG_PATH
    # Temporarily swap CONFIG_PATH for _ensure_config if an override is given.
    if keywords_config:
        original = Path(keywords_config)
        if original.exists():
            keywords = _load_yaml(str(original)) or dict(DEFAULT_KEYWORDS)
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_text(_dump_yaml(DEFAULT_KEYWORDS), encoding="utf-8")
            keywords = dict(DEFAULT_KEYWORDS)
    else:
        keywords = _ensure_config()

    result: Dict[str, Any] = {
        "scanned_keywords": len(keywords),
        "total_matches": 0,
        "new_matches": 0,
        "ledger_items_created": [],
        "matches": [],
        "dry_run": dry_run,
        "config_path": str(config_path),
    }

    conn = _open_corpus(DB_PATH)
    if conn is None:
        result["error"] = f"tweet corpus not found at {DB_PATH}"
        return result

    now = time.time()
    state = _load_state()
    purged = _purge_stale(state, now)
    result["seen_purged"] = purged

    try:
        add_item = None
        if not dry_run:
            from ai.ledger_manager import add_item as _add_item  # lazy import
            add_item = _add_item

        for keyword, meta in keywords.items():
            priority = meta.get("priority", "P1")
            venture = meta.get("venture", "delimit")

            try:
                rows = _query_keyword(conn, keyword)
            except Exception:
                rows = []

            result["total_matches"] += len(rows)

            for row in rows:
                tweet_id = str(row["tweet_id"])
                dedup_key = f"{tweet_id}:{keyword}"
                if dedup_key in state["seen"]:
                    continue

                author = str(row["author_handle"] or "unknown")
                text = str(row["text"] or "")
                created_at = str(row["created_at"] or "")
                likes = int(row["like_count"] or 0)

                title = _format_title(keyword, author, likes)
                description = _format_description(
                    keyword, tweet_id, author, text, created_at, likes, venture
                )

                match_record = {
                    "tweet_id": tweet_id,
                    "keyword": keyword,
                    "author": author,
                    "likes": likes,
                    "priority": priority,
                    "venture": venture,
                }

                if not dry_run and add_item is not None:
                    try:
                        added = add_item(
                            title=title,
                            ledger="ops",
                            type="signal",
                            priority=priority,
                            description=description,
                            source="sensor_twttr",
                            tags=["sensor", "twttr241", keyword, venture],
                        )
                        item_id = added.get("added", {}).get("id")
                        if item_id:
                            result["ledger_items_created"].append(item_id)
                            match_record["ledger_id"] = item_id
                    except Exception as exc:
                        match_record["error"] = f"ledger_add_failed: {exc}"

                state["seen"][dedup_key] = now
                result["new_matches"] += 1
                result["matches"].append(match_record)
    finally:
        conn.close()

    if not dry_run:
        _save_state(state)

    return result


__all__ = ["delimit_sensor_twttr", "DEFAULT_KEYWORDS"]
