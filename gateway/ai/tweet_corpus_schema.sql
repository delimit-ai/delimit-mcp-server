-- Tweet corpus + cache + budget schema
-- See DECISION_TWTTR241_CORPUS.md
-- Invariants:
--   tweets = append-only moat, never purged
--   cache  = disposable, TTL-gated
--   budget = single gate for all Twttr241 HTTP calls

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Corpus (moat, never purged)
CREATE TABLE IF NOT EXISTS tweets (
  tweet_id TEXT PRIMARY KEY,
  author_handle TEXT NOT NULL,
  author_id TEXT,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  fetched_at INTEGER NOT NULL,
  lang TEXT,
  reply_to_id TEXT,
  quote_of_id TEXT,
  like_count INTEGER,
  retweet_count INTEGER,
  reply_count INTEGER,
  view_count INTEGER,
  has_media INTEGER,
  urls_json TEXT,
  hashtags_json TEXT,
  mentions_json TEXT,
  venture_tags TEXT,           -- comma-joined, e.g. 'delimit,wirereport'
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tweets_author_time ON tweets(author_handle, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_venture ON tweets(venture_tags);

-- Full-text search over the corpus (contentless external-content pattern)
CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
  text, author_handle,
  content='tweets', content_rowid='rowid'
);

-- Users (opportunistic)
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  display_name TEXT,
  bio TEXT,
  followers_count INTEGER,
  following_count INTEGER,
  first_seen_at INTEGER NOT NULL,
  last_refreshed_at INTEGER NOT NULL,
  raw_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_handle ON users(handle);

-- Cache (disposable, TTL-gated)
CREATE TABLE IF NOT EXISTS cache (
  cache_key TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL,
  response_json TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);

-- Budget tracker (one row per hour bucket)
CREATE TABLE IF NOT EXISTS budget (
  hour_bucket INTEGER PRIMARY KEY,
  day_bucket INTEGER NOT NULL,
  month_bucket TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  hit_429 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_budget_day ON budget(day_bucket);
CREATE INDEX IF NOT EXISTS idx_budget_month ON budget(month_bucket);
