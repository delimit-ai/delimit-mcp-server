import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.reddit_proxy")

# LED-2068: freshness ceiling. PullPush stopped ingesting around 2025-05-19;
# the residential proxy gets 403 from Reddit on datacenter IPs; direct fetch
# is blocked. ALL three tiers can return stale archive data on any given
# fetch, and stale data is worse than no data for engagement discovery
# (drafting against year-old threads burns trust). Default to a 14-day
# freshness ceiling — anything older is dropped before returning.
#
# Override via DELIMIT_REDDIT_MAX_AGE_DAYS (set to a large number to disable).
DEFAULT_MAX_AGE_DAYS = 14
TIER_PROXY = "proxy"
TIER_PULLPUSH = "pullpush"
TIER_DIRECT = "direct"


def _max_age_seconds() -> float:
    raw = os.environ.get("DELIMIT_REDDIT_MAX_AGE_DAYS", "").strip()
    if raw:
        try:
            return max(0.0, float(raw)) * 86400.0
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_DAYS * 86400.0


def _stamp_and_filter(posts: List[Dict[str, Any]], tier: str, subreddit: str) -> List[Dict[str, Any]]:
    """Tag each post with _source_tier and drop anything older than the
    freshness ceiling. Returns kept posts. Also logs the drop count for
    debugging stale-archive regressions (LED-2068)."""
    if not posts:
        return []
    now = time.time()
    max_age = _max_age_seconds()
    if max_age <= 0:
        cutoff = 0.0
    else:
        cutoff = now - max_age
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for p in posts:
        try:
            created = float(p.get("created_utc") or 0)
        except (TypeError, ValueError):
            created = 0.0
        if created and created >= cutoff:
            p["_source_tier"] = tier
            kept.append(p)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "reddit_proxy: dropped %d/%d stale post(s) from %s tier for r/%s "
            "(freshness ceiling=%.1fd)",
            dropped, len(posts), tier, subreddit, max_age / 86400.0,
        )
    return kept

def _get_proxy_config() -> Dict[str, str]:
    """Load proxy config from private secrets or environment.

    Returns {proxy_url, token}. The server-side proxy requires a bearer
    token (LED-988 follow-up) — clients without a token still populate
    proxy_url but will fail auth at the server unless the server is run
    without a token (not recommended).
    """
    config = {"proxy_url": "", "token": ""}

    # 1. Environment variables
    env_url = os.environ.get("DELIMIT_REDDIT_PROXY")
    env_token = os.environ.get("DELIMIT_REDDIT_PROXY_TOKEN")
    if env_url:
        config["proxy_url"] = env_url
    if env_token:
        config["token"] = env_token
    if config["proxy_url"]:
        return config

    # 2. Secrets file
    secrets_path = Path.home() / ".delimit" / "secrets" / "reddit-proxy.json"
    if secrets_path.exists():
        try:
            secrets = json.loads(secrets_path.read_text())
            config["proxy_url"] = secrets.get("proxy_url", "") or config["proxy_url"]
            config["token"] = secrets.get("token", "") or config["token"]
        except Exception as e:
            logger.debug(f"Failed to load reddit-proxy secrets: {e}")

    return config

def fetch_subreddit(subreddit: str, sort: str = "new", limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch posts from a single subreddit with fallback chain.
    Returns standardized post dicts. Each post is tagged with _source_tier
    indicating which fallback served it, and stale posts (older than the
    freshness ceiling per LED-2068) are dropped before returning.
    """
    reddit_url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"

    # 1. Try Local Proxy (Residential IP)
    proxy_cfg = _get_proxy_config()
    proxy_url = proxy_cfg.get("proxy_url")
    if proxy_url:
        try:
            fetch_url = f"{proxy_url}?url={urllib.parse.quote(reddit_url, safe='')}"
            headers = {"User-Agent": "Delimit/1.0"}
            # nosec B105 — reads proxy auth credential from config, not a hardcoded secret
            auth_token = proxy_cfg.get("token", "")
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
            req = urllib.request.Request(fetch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
                children = body.get("data", {}).get("children", [])
                raw = [c.get("data", {}) for c in children if c.get("data")]
                kept = _stamp_and_filter(raw, TIER_PROXY, subreddit)
                if kept:
                    return kept
                # If the proxy succeeded but returned only stale data, fall
                # through to next tier rather than returning empty — gives
                # us a chance to find fresh data elsewhere.
        except Exception as e:
            logger.debug(f"Local proxy failed for r/{subreddit}: {e}")

    # 3. Try Direct (often blocked on datacenter IPs, but fast when it works
    # and is the only tier currently capable of serving fresh data — PullPush
    # stopped ingesting ~2025-05-19, residential proxy 403s from datacenter).
    # Direct moved AHEAD of PullPush in the chain post-LED-2068 because a
    # blocked direct fetch is recoverable via fallback, while a successful
    # PullPush serves stale archive that pollutes downstream classifiers.
    try:
        req = urllib.request.Request(reddit_url, headers={"User-Agent": "Mozilla/5.0 (Delimit)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            children = body.get("data", {}).get("children", [])
            raw = [c.get("data", {}) for c in children if c.get("data")]
            kept = _stamp_and_filter(raw, TIER_DIRECT, subreddit)
            if kept:
                return kept
    except Exception as e:
        logger.debug(f"Direct fetch failed for r/{subreddit}: {e}")

    # 2. Last-resort: PullPush archive. Currently stale (May 2025 ceiling)
    # but the freshness filter will drop everything if so — leaves the door
    # open for the day PullPush resumes ingesting fresh data.
    try:
        pp_url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={subreddit}&size={limit}&sort=desc"
        req = urllib.request.Request(pp_url, headers={"User-Agent": "Delimit/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            raw = body.get("data", []) or []
            kept = _stamp_and_filter(raw, TIER_PULLPUSH, subreddit)
            if kept:
                return kept
    except Exception as e:
        logger.debug(f"PullPush fallback failed for r/{subreddit}: {e}")

    return []

def fetch_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single Reddit thread by ID with fallback chain.
    """
    reddit_url = f"https://www.reddit.com/comments/{thread_id}.json?raw_json=1"
    
    # 1. Try Local Proxy
    proxy_cfg = _get_proxy_config()
    proxy_url = proxy_cfg.get("proxy_url")
    if proxy_url:
        try:
            fetch_url = f"{proxy_url}?url={urllib.parse.quote(reddit_url, safe='')}"
            headers = {"User-Agent": "Delimit/1.0"}
            # nosec B105 — reads proxy auth credential from config, not a hardcoded secret
            auth_token = proxy_cfg.get("token", "")
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
            req = urllib.request.Request(fetch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list) and len(data) > 0:
                    return data[0].get("data", {}).get("children", [{}])[0].get("data", {})
        except Exception as e:
            logger.debug(f"Local proxy failed for thread {thread_id}: {e}")

    # 2. Fallback: PullPush
    try:
        pp_url = f"https://api.pullpush.io/reddit/search/submission/?ids={thread_id}"
        req = urllib.request.Request(pp_url, headers={"User-Agent": "Delimit/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            data = body.get("data", [])
            return data[0] if data else None
    except Exception as e:
        logger.debug(f"PullPush fallback failed for thread {thread_id}: {e}")

    return None
