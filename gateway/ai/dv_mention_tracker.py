"""DomainVested mention tracker.

Nightly FTS5 scan over the delimit tweet corpus (LED-766) for mentions of
Jamsons Holdings portfolio domains. Writes matches to a dedicated
`domain_mentions` sidecar table in the same tweets.db. Zero API calls —
pure read over the existing corpus.

Contract with LED-766:
  * READ-ONLY over ``tweets`` and ``tweets_fts``.
  * WRITE-ONLY to ``domain_mentions`` (created here, idempotent).
  * No edits to ``tweet_corpus.py`` — we open our own connection with the
    same WAL / synchronous=NORMAL pragmas.

See DECISION_TWTTR241_CORPUS.md.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.expanduser("~/.delimit/data/tweets.db")

# Jamsons Holdings portfolio — extend as domains are added.
PORTFOLIO_DOMAINS: list[str] = [
    "delimit.ai",
    "domainvested.com",
    "wire.report",
    "livetube.ai",
    "stake.one",
    "validator.one",
    "jamsons.io",
    "infracore",  # alias for GitHub org mentions
]

# Idempotent schema extension.
DOMAIN_MENTIONS_DDL = """
CREATE TABLE IF NOT EXISTS domain_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    author_handle TEXT NOT NULL,
    tweet_text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    like_count INTEGER,
    retweet_count INTEGER,
    sentiment_placeholder TEXT,
    UNIQUE(tweet_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_dv_mentions_domain
    ON domain_mentions(domain, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_dv_mentions_created
    ON domain_mentions(created_at DESC);
"""


# --------------------------------------------------------------------- utils


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a connection using the same pragmas as tweet_corpus.py."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _fts_keyword(domain: str) -> str:
    """Convert a portfolio entry into an FTS5-safe keyword.

    FTS5's default tokenizer splits on punctuation, so ``delimit.ai`` yields
    tokens ``delimit`` and ``ai``. We pick the most distinctive token for a
    coarse FTS prefilter, then Python-side confirm the full domain string
    appears in the tweet text. ``infracore`` has no dot so passes through.
    """
    # Strip the TLD when present — the left label is what people actually type.
    if "." in domain:
        left = domain.split(".", 1)[0]
        return left.lower()
    return domain.lower()


def _domain_in_text(domain: str, text: str) -> bool:
    """Case-insensitive substring check over the full tweet text."""
    if not text:
        return False
    return domain.lower() in text.lower()


# -------------------------------------------------------------------- schema


def ensure_schema(db_path: str = DB_PATH) -> None:
    """Idempotent schema migration. Safe to call on every nightly run.

    If the database file does not exist we create its parent directory but
    do NOT bootstrap the upstream ``tweets`` schema — that belongs to
    LED-766. The sidecar table is created regardless so a scan can no-op
    cleanly when the corpus is empty.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(DOMAIN_MENTIONS_DDL)
        conn.commit()
    finally:
        conn.close()


def _corpus_available(conn: sqlite3.Connection) -> bool:
    """Check that the upstream corpus tables exist before we scan."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','view') AND name='tweets'"
    ).fetchone()
    if row is None:
        return False
    fts = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='tweets_fts'"
    ).fetchone()
    return fts is not None


# ---------------------------------------------------------------------- scan


def scan_portfolio_mentions(
    domains: Optional[list[str]] = None,
    since_hours: int = 24,
    db_path: str = DB_PATH,
) -> dict:
    """Nightly scan: for each portfolio domain, find new tweets mentioning
    it within the last ``since_hours`` and record them in
    ``domain_mentions``. Re-runs are idempotent via the
    ``UNIQUE(tweet_id, domain)`` constraint.
    """
    ran_at = int(time.time())
    window_start = ran_at - int(since_hours) * 3600
    target_domains = list(domains) if domains else list(PORTFOLIO_DOMAINS)

    result = {
        "domains_scanned": len(target_domains),
        "total_matches": 0,
        "new_mentions": 0,
        "duplicates_skipped": 0,
        "by_domain": {d: 0 for d in target_domains},
        "window_hours": int(since_hours),
        "ran_at": ran_at,
    }

    if not os.path.exists(db_path):
        logger.warning("dv_mention_tracker: tweets.db missing at %s", db_path)
        return result

    # Ensure our sidecar table exists before we write.
    ensure_schema(db_path)

    conn = _connect(db_path)
    try:
        if not _corpus_available(conn):
            logger.warning(
                "dv_mention_tracker: corpus tables not initialised in %s",
                db_path,
            )
            return result

        select_sql = (
            "SELECT t.tweet_id, t.author_handle, t.text, t.created_at, "
            "       t.like_count, t.retweet_count "
            "FROM tweets t "
            "JOIN tweets_fts fts ON fts.rowid = t.rowid "
            "WHERE tweets_fts MATCH ? "
            "  AND t.created_at >= ? "
            "ORDER BY t.created_at DESC"
        )
        insert_sql = (
            "INSERT OR IGNORE INTO domain_mentions ("
            "tweet_id, domain, author_handle, tweet_text, created_at, "
            "fetched_at, like_count, retweet_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )

        cur = conn.cursor()
        for domain in target_domains:
            keyword = _fts_keyword(domain)
            try:
                rows = cur.execute(select_sql, (keyword, window_start)).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "dv_mention_tracker: FTS query failed for %s (%s)",
                    domain,
                    exc,
                )
                continue

            for row in rows:
                text = row["text"] or ""
                # Python-side confirm to avoid FTS tokenisation false positives
                # (``delimit`` alone would otherwise match un-related tweets).
                if not _domain_in_text(domain, text):
                    continue

                result["total_matches"] += 1
                cur.execute(
                    insert_sql,
                    (
                        row["tweet_id"],
                        domain,
                        row["author_handle"] or "",
                        text,
                        int(row["created_at"] or 0),
                        ran_at,
                        row["like_count"],
                        row["retweet_count"],
                    ),
                )
                if cur.rowcount == 1:
                    result["new_mentions"] += 1
                    result["by_domain"][domain] += 1
                else:
                    result["duplicates_skipped"] += 1
        conn.commit()
    finally:
        conn.close()

    return result


# -------------------------------------------------------------------- reads


def recent_mentions(
    domain: str,
    limit: int = 50,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return the most recent mentions for ``domain``, newest first."""
    if not os.path.exists(db_path):
        return []
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tweet_id, domain, author_handle, tweet_text, created_at, "
            "       fetched_at, like_count, retweet_count, sentiment_placeholder "
            "FROM domain_mentions "
            "WHERE domain = ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (domain, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mention_summary(since_hours: int = 168, db_path: str = DB_PATH) -> dict:
    """Rolling window summary: mentions per domain, top authors, totals."""
    window_hours = int(since_hours)
    result = {
        "window_hours": window_hours,
        "by_domain": {},
        "total_mentions": 0,
        "unique_authors": 0,
    }
    if not os.path.exists(db_path):
        return result
    ensure_schema(db_path)
    cutoff = int(time.time()) - window_hours * 3600

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT domain, author_handle "
            "FROM domain_mentions "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()

        by_domain: dict[str, dict] = {}
        all_authors: set[str] = set()
        for row in rows:
            domain = row["domain"]
            author = row["author_handle"] or ""
            bucket = by_domain.setdefault(
                domain, {"count": 0, "_authors": {}}
            )
            bucket["count"] += 1
            if author:
                bucket["_authors"][author] = bucket["_authors"].get(author, 0) + 1
                all_authors.add(author)
            result["total_mentions"] += 1

        for domain, bucket in by_domain.items():
            authors_sorted = sorted(
                bucket["_authors"].items(), key=lambda kv: kv[1], reverse=True
            )
            result["by_domain"][domain] = {
                "count": bucket["count"],
                "top_authors": [
                    {"handle": h, "mentions": c} for h, c in authors_sorted[:5]
                ],
            }
        result["unique_authors"] = len(all_authors)
    finally:
        conn.close()

    return result
