"""
WireIntel X signal consumer.

Reads from the delimit tweet corpus (filtered by venture_tags='wirereport')
and returns sport-keyed signals. No new ingestion — relies on scan_targets
with venture_tag='wirereport' (LED-767) populating the shared corpus.

READ-ONLY over the tweet corpus. See DECISION_TWTTR241_CORPUS.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from typing import Optional

DB_PATH = os.path.expanduser("~/.delimit/data/tweets.db")

# Sport-specific FTS5 keyword sets
SPORT_KEYWORDS = {
    "nba": ["nba", "basketball", "lakers", "celtics", "warriors", "LeBron", "Curry"],
    "nfl": ["nfl", "football", "touchdown", "Patriots", "Chiefs", "Cowboys"],
    "mlb": ["mlb", "baseball", "homerun", "Yankees", "Dodgers", "World Series"],
    "nhl": ["nhl", "hockey", "Stanley Cup", "Rangers", "Bruins"],
    "wnba": ["wnba", "women's basketball"],
    "f1": ["f1", "formula 1", "grand prix", "Verstappen", "Hamilton"],
}

# Trusted sports reporters / handles that publish breaking news
SPORTS_REPORTER_HANDLES = {
    "ShamsCharania", "wojespn", "AdamSchefter", "RapSheet",
    "FabrizioRomano", "TheAthletic", "ESPN", "BleacherReport",
}

# Score weights
_REPORTER_BONUS = 5
_RETWEET_WEIGHT = 2
_LIKE_WEIGHT = 1

# Mandatory filter — every query must apply this
_WIREREPORT_FILTER = "t.venture_tags LIKE '%wirereport%'"


def _get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a read-only-ish connection to the tweet corpus."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Tweet corpus DB not found at {path}. "
            "WireIntel X requires the shared corpus populated by LED-767 scan_targets."
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _build_match_expr(sport: str) -> str:
    """Build an FTS5 MATCH expression OR'ing sport keywords."""
    if sport == "all":
        keywords: list[str] = []
        for kws in SPORT_KEYWORDS.values():
            keywords.extend(kws)
    else:
        if sport not in SPORT_KEYWORDS:
            raise ValueError(
                f"Unknown sport '{sport}'. Valid: {sorted(SPORT_KEYWORDS.keys())} or 'all'"
            )
        keywords = SPORT_KEYWORDS[sport]
    # Quote each keyword for FTS5 safety, join with OR
    return " OR ".join(f'"{k}"' for k in keywords)


def _score_row(row: sqlite3.Row, is_reporter: bool) -> float:
    likes = row["like_count"] or 0
    rts = row["retweet_count"] or 0
    base = _LIKE_WEIGHT * likes + _RETWEET_WEIGHT * rts
    if is_reporter:
        base += _REPORTER_BONUS
    return float(base)


def _row_to_signal(row: sqlite3.Row) -> dict:
    author = row["author_handle"]
    is_reporter = author in SPORTS_REPORTER_HANDLES
    return {
        "tweet_id": row["tweet_id"],
        "author": author,
        "text": row["text"],
        "created_at": row["created_at"],
        "like_count": row["like_count"] or 0,
        "retweet_count": row["retweet_count"] or 0,
        "is_reporter": is_reporter,
        "url": f"https://twitter.com/{author}/status/{row['tweet_id']}",
        "score": _score_row(row, is_reporter),
    }


def wireintel_query_x(
    sport: str,
    since: Optional[int] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> list[dict]:
    """
    Query the tweet corpus for sport-specific signals.

    Sorted by score DESC. Every query is filtered by venture_tags LIKE '%wirereport%'.
    """
    match_expr = _build_match_expr(sport)
    since_val = int(since) if since is not None else 0
    conn = _get_conn(db_path)
    try:
        sql = f"""
            SELECT t.tweet_id, t.author_handle, t.text, t.created_at,
                   t.like_count, t.retweet_count, t.venture_tags,
                   t.hashtags_json
            FROM tweets t
            JOIN tweets_fts fts ON fts.rowid = t.rowid
            WHERE tweets_fts MATCH ?
              AND {_WIREREPORT_FILTER}
              AND t.created_at >= ?
            ORDER BY t.created_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (match_expr, since_val, int(limit))).fetchall()
    finally:
        conn.close()

    signals = [_row_to_signal(r) for r in rows]
    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals


def wireintel_breaking_news(
    sport: str,
    since_minutes: int = 60,
    db_path: Optional[str] = None,
) -> list[dict]:
    """
    Shortcut for recent reporter signals. Filters to SPORTS_REPORTER_HANDLES only,
    returns results from the last N minutes sorted by created_at DESC.

    Still applies venture_tags LIKE '%wirereport%' filter.
    """
    match_expr = _build_match_expr(sport)
    since_ts = int(time.time()) - (int(since_minutes) * 60)
    if not SPORTS_REPORTER_HANDLES:
        return []

    placeholders = ",".join("?" for _ in SPORTS_REPORTER_HANDLES)
    handles = tuple(sorted(SPORTS_REPORTER_HANDLES))

    conn = _get_conn(db_path)
    try:
        sql = f"""
            SELECT t.tweet_id, t.author_handle, t.text, t.created_at,
                   t.like_count, t.retweet_count, t.venture_tags,
                   t.hashtags_json
            FROM tweets t
            JOIN tweets_fts fts ON fts.rowid = t.rowid
            WHERE tweets_fts MATCH ?
              AND {_WIREREPORT_FILTER}
              AND t.created_at >= ?
              AND t.author_handle IN ({placeholders})
            ORDER BY t.created_at DESC
        """
        rows = conn.execute(sql, (match_expr, since_ts, *handles)).fetchall()
    finally:
        conn.close()

    return [_row_to_signal(r) for r in rows]


def wireintel_sport_trending(
    sport: str,
    window_hours: int = 24,
    db_path: Optional[str] = None,
) -> dict:
    """
    Aggregate signal for a sport over a time window.

    Applies venture_tags LIKE '%wirereport%' on every lookup.
    """
    match_expr = _build_match_expr(sport)
    since_ts = int(time.time()) - (int(window_hours) * 3600)

    conn = _get_conn(db_path)
    try:
        sql = f"""
            SELECT t.tweet_id, t.author_handle, t.text, t.created_at,
                   t.like_count, t.retweet_count, t.venture_tags,
                   t.hashtags_json
            FROM tweets t
            JOIN tweets_fts fts ON fts.rowid = t.rowid
            WHERE tweets_fts MATCH ?
              AND {_WIREREPORT_FILTER}
              AND t.created_at >= ?
            ORDER BY t.created_at DESC
        """
        rows = conn.execute(sql, (match_expr, since_ts)).fetchall()
    finally:
        conn.close()

    signals = [_row_to_signal(r) for r in rows]
    total = len(signals)

    # Top 10 by likes
    top_tweets = sorted(signals, key=lambda s: s["like_count"], reverse=True)[:10]

    # Top 5 authors by volume
    author_counts = Counter(s["author"] for s in signals)
    top_authors = [
        {"author": a, "count": c} for a, c in author_counts.most_common(5)
    ]

    # Hashtag frequency
    hashtag_counter: Counter[str] = Counter()
    for r in rows:
        raw = r["hashtags_json"]
        if not raw:
            continue
        try:
            tags = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag:
                    hashtag_counter[tag.lower()] += 1
    top_hashtags = [
        {"tag": t, "count": c} for t, c in hashtag_counter.most_common(10)
    ]

    return {
        "sport": sport,
        "window_hours": int(window_hours),
        "total_signals": total,
        "top_tweets": top_tweets,
        "top_authors": top_authors,
        "top_hashtags": top_hashtags,
    }
