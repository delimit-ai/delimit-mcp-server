"""Tweet corpus DAL.

Single SQLite file at ~/.delimit/data/tweets.db holding:
  - tweets (append-only moat, never purged)
  - tweets_fts (FTS5 external-content mirror)
  - users (opportunistic)
  - cache (disposable, TTL-gated)
  - budget (hourly buckets, single gate for all Twttr241 calls)

See DECISION_TWTTR241_CORPUS.md. No ORM — stdlib sqlite3 only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# LED-797: default Delimit ground-truth keyword set used by topic_intersect_delimit.
DEFAULT_DELIMIT_KEYWORDS: list[str] = [
    "openapi",
    "breaking change",
    "api governance",
    "semver",
    "mcp server",
    "mcp",
    "context persistence",
    "cross-model",
    "cross model",
    "memory",
    "audit trail",
    "ci",
    "github action",
    "claude code",
    "codex",
    "cursor",
    "gemini cli",
    "agent harness",
    "portability",
]

# Cheap English stopwords for topic clustering — intentionally short.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "from", "as", "at", "by", "is", "it", "this", "that", "these",
    "those", "be", "was", "were", "are", "am", "has", "have", "had",
    "not", "no", "yes", "i", "you", "he", "she", "we", "they", "me",
    "him", "her", "us", "them", "my", "your", "our", "their",
    "so", "do", "does", "did", "can", "could", "should", "would", "will",
    "just", "also", "then", "than", "too", "very", "like", "about", "out",
    "up", "down", "over", "under", "after", "before", "while", "because",
    "https", "http", "www", "com", "t", "co", "io", "re", "ve", "ll", "s",
    "m", "if", "all", "any", "some", "how", "why", "what", "when", "where",
    "who", "which", "get", "got", "now", "new", "one", "two", "other",
    "more", "most", "its", "your", "his", "our", "via", "said", "says",
    "here", "there", "into", "onto", "us", "rt", "amp",
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9'-]+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#([a-z0-9_]+)", re.IGNORECASE)


def _iter_hashtags(hashtags_json: str | None, text: str):
    """Yield normalized hashtag keywords from the stored json column + raw text."""
    seen: set[str] = set()
    if hashtags_json:
        try:
            data = json.loads(hashtags_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    tag = entry.get("tag") or entry.get("name") or ""
                else:
                    tag = str(entry)
                tag = tag.strip().lstrip("#").lower()
                if tag and tag not in seen:
                    seen.add(tag)
                    yield f"#{tag}"
    for match in _HASHTAG_RE.findall(text or ""):
        tag = match.lower()
        if tag and tag not in seen:
            seen.add(tag)
            yield f"#{tag}"

SCHEMA_PATH = Path(__file__).with_name("tweet_corpus_schema.sql")
DEFAULT_DB_PATH = os.path.expanduser("~/.delimit/data/tweets.db")

# Budget tiers (100k/mo ÷ 730hr ≈ 137/hr nominal ceiling)
HOURLY_CEILING = 137
DAILY_FREEZE = 3200
MONTHLY_FREEZE = 95000


class TweetCorpus:
    """Data access layer for the Twttr241 corpus + cache + budget."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ init

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            if SCHEMA_PATH.exists():
                sql = SCHEMA_PATH.read_text()
            else:
                sql = _INLINE_SCHEMA
            self._conn.executescript(sql)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ corpus

    def upsert_tweets(self, tweets: list[dict], venture_tag: str) -> dict:
        """Idempotent upsert. New rows inserted; existing rows get engagement
        refreshed and venture_tags merged. fetched_at is preserved on conflict.
        """
        inserted = 0
        updated = 0
        fts_rows = 0
        now = int(time.time())
        with self._lock:
            cur = self._conn.cursor()
            for t in tweets:
                tid = str(t.get("tweet_id") or t.get("id") or "").strip()
                if not tid:
                    continue
                existing = cur.execute(
                    "SELECT rowid, venture_tags FROM tweets WHERE tweet_id=?",
                    (tid,),
                ).fetchone()
                if existing is None:
                    row = (
                        tid,
                        t.get("author_handle") or "",
                        t.get("author_id"),
                        t.get("text") or "",
                        int(t.get("created_at") or now),
                        now,
                        t.get("lang"),
                        t.get("reply_to_id"),
                        t.get("quote_of_id"),
                        _int(t.get("like_count")),
                        _int(t.get("retweet_count")),
                        _int(t.get("reply_count")),
                        _int(t.get("view_count")),
                        1 if t.get("has_media") else 0,
                        _json(t.get("urls")),
                        _json(t.get("hashtags")),
                        _json(t.get("mentions")),
                        venture_tag,
                        _json(t.get("raw")) if t.get("raw") is not None else None,
                    )
                    cur.execute(
                        """INSERT INTO tweets (
                            tweet_id, author_handle, author_id, text, created_at,
                            fetched_at, lang, reply_to_id, quote_of_id,
                            like_count, retweet_count, reply_count, view_count,
                            has_media, urls_json, hashtags_json, mentions_json,
                            venture_tags, raw_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        row,
                    )
                    rowid = cur.lastrowid
                    cur.execute(
                        "INSERT INTO tweets_fts(rowid, text, author_handle) VALUES (?,?,?)",
                        (rowid, row[3], row[1]),
                    )
                    inserted += 1
                    fts_rows += 1
                else:
                    merged = _merge_tags(existing["venture_tags"], venture_tag)
                    cur.execute(
                        """UPDATE tweets SET
                             like_count=COALESCE(?, like_count),
                             retweet_count=COALESCE(?, retweet_count),
                             reply_count=COALESCE(?, reply_count),
                             view_count=COALESCE(?, view_count),
                             venture_tags=?
                           WHERE tweet_id=?""",
                        (
                            _int(t.get("like_count")),
                            _int(t.get("retweet_count")),
                            _int(t.get("reply_count")),
                            _int(t.get("view_count")),
                            merged,
                            tid,
                        ),
                    )
                    updated += 1
            self._conn.commit()
        return {"inserted": inserted, "updated": updated, "fts_rows": fts_rows}

    # -------------------------------------------------------------- cache

    def get_cached(self, cache_key: str) -> dict | None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT response_json, expires_at FROM cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now:
            return None
        try:
            return json.loads(row["response_json"])
        except json.JSONDecodeError:
            return None

    def put_cache(
        self, cache_key: str, endpoint: str, response: dict, ttl_seconds: int
    ) -> None:
        now = int(time.time())
        expires = now + int(ttl_seconds)
        payload = json.dumps(response, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO cache
                   (cache_key, endpoint, response_json, fetched_at, expires_at)
                   VALUES (?,?,?,?,?)""",
                (cache_key, endpoint, payload, now, expires),
            )
            self._conn.commit()

    def purge_expired_cache(self) -> int:
        cutoff = int(time.time()) - 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cache WHERE expires_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------- budget

    def record_api_call(self, endpoint: str, success: bool, status: int | None = None) -> None:
        now = int(time.time())
        hour_bucket = now // 3600
        day_bucket = now // 86400
        month_bucket = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")
        hit_429_delta = 1 if (not success and status == 429) else 0
        with self._lock:
            cur = self._conn.cursor()
            existing = cur.execute(
                "SELECT hour_bucket FROM budget WHERE hour_bucket=?",
                (hour_bucket,),
            ).fetchone()
            if existing is None:
                cur.execute(
                    """INSERT INTO budget
                       (hour_bucket, day_bucket, month_bucket, requests, hit_429)
                       VALUES (?,?,?,?,?)""",
                    (hour_bucket, day_bucket, month_bucket, 1, hit_429_delta),
                )
            else:
                cur.execute(
                    """UPDATE budget
                       SET requests = requests + 1,
                           hit_429 = hit_429 + ?
                       WHERE hour_bucket=?""",
                    (hit_429_delta, hour_bucket),
                )
            self._conn.commit()

    def check_budget(self) -> dict:
        now = int(time.time())
        hour_bucket = now // 3600
        day_bucket = now // 86400
        month_bucket = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")
        with self._lock:
            hour_row = self._conn.execute(
                "SELECT COALESCE(SUM(requests), 0) AS n FROM budget WHERE hour_bucket=?",
                (hour_bucket,),
            ).fetchone()
            day_row = self._conn.execute(
                "SELECT COALESCE(SUM(requests), 0) AS n FROM budget WHERE day_bucket=?",
                (day_bucket,),
            ).fetchone()
            month_row = self._conn.execute(
                "SELECT COALESCE(SUM(requests), 0) AS n FROM budget WHERE month_bucket=?",
                (month_bucket,),
            ).fetchone()
        hour_requests = int(hour_row["n"])
        day_requests = int(day_row["n"])
        month_requests = int(month_row["n"])
        usage_pct = hour_requests / HOURLY_CEILING if HOURLY_CEILING else 0.0

        if month_requests >= MONTHLY_FREEZE:
            mode, allowed = "monthly-freeze", False
        elif day_requests >= DAILY_FREEZE:
            mode, allowed = "daily-freeze", False
        elif usage_pct >= 0.95:
            mode, allowed = "hard-stop", False
        elif usage_pct >= 0.85:
            mode, allowed = "founder-priority", True
        elif usage_pct >= 0.70:
            mode, allowed = "throttle", True
        else:
            mode, allowed = "normal", True

        return {
            "hour_requests": hour_requests,
            "day_requests": day_requests,
            "month_requests": month_requests,
            "usage_pct": usage_pct,
            "mode": mode,
            "allowed": allowed,
        }

    # ----------------------------------------------- search / topic radar
    # LED-784 + LED-797: FTS5 search, topic clustering, Delimit intersect.

    def search_fts(self, query: str, limit: int = 50) -> list[dict]:
        """FTS5 MATCH over (text, author_handle) returning hydrated tweet rows.

        Accepts the raw FTS5 query syntax — callers are responsible for
        escaping user input. Returns rows sorted by created_at DESC.
        """
        if not query or not query.strip():
            return []
        with self._lock:
            cur = self._conn.execute(
                """SELECT t.*
                     FROM tweets_fts f
                     JOIN tweets t ON t.rowid = f.rowid
                    WHERE tweets_fts MATCH ?
                 ORDER BY t.created_at DESC
                    LIMIT ?""",
                (query, int(limit)),
            )
            return [dict(row) for row in cur.fetchall()]

    def topic_cluster(
        self,
        since_hours: int = 72,
        min_cluster_size: int = 3,
        keywords: list[str] | None = None,
    ) -> list[dict]:
        """Cluster tweets by shared keywords/hashtags over a trailing window.

        Simple stdlib-only clustering: tokenize text, extract hashtags,
        weight by engagement (likes + retweets + replies), rank terms,
        then group tweets that mention each top term.

        Returns list sorted by total_engagement descending. Each cluster::

            {
              "keyword": "openapi",
              "tweet_ids": [...],
              "total_engagement": 1234,
              "top_authors": ["alice", "bob", ...],
              "sample_tweets": [
                {"tweet_id": "...", "author_handle": "...", "text": "...",
                 "engagement": 42, "created_at": 1234567890}
              ],
            }
        """
        cutoff = int(time.time()) - max(1, int(since_hours)) * 3600
        with self._lock:
            cur = self._conn.execute(
                """SELECT tweet_id, author_handle, text, created_at,
                          COALESCE(like_count,0) AS likes,
                          COALESCE(retweet_count,0) AS retweets,
                          COALESCE(reply_count,0) AS replies,
                          hashtags_json
                     FROM tweets
                    WHERE created_at >= ?
                 ORDER BY created_at DESC""",
                (cutoff,),
            )
            rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            return []

        # Normalize keyword filter
        allowed_kw: set[str] | None = None
        if keywords:
            allowed_kw = {k.strip().lower() for k in keywords if k and k.strip()}

        term_stats: dict[str, dict] = {}

        for row in rows:
            text = (row.get("text") or "").lower()
            engagement = int(row["likes"]) + int(row["retweets"]) + int(row["replies"])
            terms: set[str] = set()

            # Hashtags first (cheaper + more intentional signal)
            for tag in _iter_hashtags(row.get("hashtags_json"), text):
                terms.add(tag)

            # 1-gram and 2-gram word extraction
            tokens = [t for t in _TOKEN_RE.findall(text) if t not in _STOPWORDS and len(t) > 2]
            terms.update(tokens)
            for i in range(len(tokens) - 1):
                bigram = f"{tokens[i]} {tokens[i+1]}"
                terms.add(bigram)

            if allowed_kw:
                terms = {t for t in terms if any(kw in t for kw in allowed_kw)}

            for term in terms:
                stat = term_stats.setdefault(
                    term,
                    {
                        "keyword": term,
                        "tweet_ids": [],
                        "total_engagement": 0,
                        "authors": {},
                        "sample_tweets": [],
                        "count": 0,
                    },
                )
                stat["tweet_ids"].append(row["tweet_id"])
                stat["total_engagement"] += engagement
                stat["count"] += 1
                stat["authors"][row["author_handle"]] = (
                    stat["authors"].get(row["author_handle"], 0) + 1
                )
                # Keep top-5 samples by engagement
                sample_entry = {
                    "tweet_id": row["tweet_id"],
                    "author_handle": row["author_handle"],
                    "text": row["text"],
                    "engagement": engagement,
                    "created_at": row["created_at"],
                }
                samples = stat["sample_tweets"]
                samples.append(sample_entry)
                if len(samples) > 5:
                    samples.sort(key=lambda s: s["engagement"], reverse=True)
                    del samples[5:]

        # Filter to min cluster size, finalize shape, sort
        clusters: list[dict] = []
        for term, stat in term_stats.items():
            if stat["count"] < min_cluster_size:
                continue
            top_authors = sorted(stat["authors"].items(), key=lambda x: x[1], reverse=True)[:5]
            clusters.append(
                {
                    "keyword": term,
                    "tweet_ids": stat["tweet_ids"][:50],
                    "total_engagement": stat["total_engagement"],
                    "top_authors": [a for a, _ in top_authors],
                    "sample_tweets": sorted(
                        stat["sample_tweets"], key=lambda s: s["engagement"], reverse=True
                    )[:5],
                    "count": stat["count"],
                }
            )

        clusters.sort(key=lambda c: c["total_engagement"], reverse=True)
        return clusters

    def topic_intersect_delimit(
        self,
        clusters: list[dict],
        ground_truth_keywords: list[str] | None = None,
    ) -> list[dict]:
        """Filter clusters to those touching Delimit's ground truth feature list."""
        gt = [k.lower() for k in (ground_truth_keywords or DEFAULT_DELIMIT_KEYWORDS)]
        if not gt:
            return []
        out: list[dict] = []
        for c in clusters:
            keyword = c.get("keyword", "").lower()
            matches: list[str] = []
            if any(g in keyword for g in gt):
                matches.append(keyword)
            # Also check sample tweet text for ground-truth term hits
            for sample in c.get("sample_tweets", []):
                stext = (sample.get("text") or "").lower()
                for g in gt:
                    if g in stext and g not in matches:
                        matches.append(g)
            if not matches:
                continue
            enriched = dict(c)
            enriched["delimit_matches"] = matches[:5]
            enriched["delimit_angle"] = (
                f"Cluster topic '{c.get('keyword', '')}' intersects Delimit ground truth "
                f"via: {', '.join(matches[:3])}"
            )
            out.append(enriched)
        return out

    # --------------------------------------------------------------- stats

    def corpus_stats(self) -> dict:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tweets"
            ).fetchone()["n"]
            authors = self._conn.execute(
                "SELECT COUNT(DISTINCT author_handle) AS n FROM tweets"
            ).fetchone()["n"]
            earliest = self._conn.execute(
                "SELECT MIN(created_at) AS n FROM tweets"
            ).fetchone()["n"]
            latest = self._conn.execute(
                "SELECT MAX(created_at) AS n FROM tweets"
            ).fetchone()["n"]
            fts_rowcount = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tweets_fts"
            ).fetchone()["n"]
        try:
            db_size = os.path.getsize(self.db_path)
        except OSError:
            db_size = 0
        return {
            "total_tweets": int(total),
            "unique_authors": int(authors),
            "earliest": earliest,
            "latest": latest,
            "fts_rowcount": int(fts_rowcount),
            "db_size_bytes": db_size,
        }


# ---------------------------------------------------------------- helpers

def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _json(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _merge_tags(existing: str | None, new: str | None) -> str | None:
    tags: list[str] = []
    for src in (existing, new):
        if not src:
            continue
        for tag in src.split(","):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return ",".join(tags) if tags else None


_INLINE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS tweets (
  tweet_id TEXT PRIMARY KEY, author_handle TEXT NOT NULL, author_id TEXT,
  text TEXT NOT NULL, created_at INTEGER NOT NULL, fetched_at INTEGER NOT NULL,
  lang TEXT, reply_to_id TEXT, quote_of_id TEXT,
  like_count INTEGER, retweet_count INTEGER, reply_count INTEGER, view_count INTEGER,
  has_media INTEGER, urls_json TEXT, hashtags_json TEXT, mentions_json TEXT,
  venture_tags TEXT, raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tweets_author_time ON tweets(author_handle, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_venture ON tweets(venture_tags);
CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
  text, author_handle, content='tweets', content_rowid='rowid'
);
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, handle TEXT NOT NULL, display_name TEXT, bio TEXT,
  followers_count INTEGER, following_count INTEGER,
  first_seen_at INTEGER NOT NULL, last_refreshed_at INTEGER NOT NULL, raw_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_handle ON users(handle);
CREATE TABLE IF NOT EXISTS cache (
  cache_key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, response_json TEXT NOT NULL,
  fetched_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
CREATE TABLE IF NOT EXISTS budget (
  hour_bucket INTEGER PRIMARY KEY, day_bucket INTEGER NOT NULL,
  month_bucket TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, hit_429 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_budget_day ON budget(day_bucket);
CREATE INDEX IF NOT EXISTS idx_budget_month ON budget(month_bucket);
"""
