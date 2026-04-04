import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.reddit_proxy")

def _get_proxy_config() -> Dict[str, str]:
    """Load proxy config from private secrets or environment."""
    config = {"proxy_url": ""}
    
    # 1. Check environment variable
    env_url = os.environ.get("DELIMIT_REDDIT_PROXY")
    if env_url:
        config["proxy_url"] = env_url
        return config

    # 2. Check private secrets file
    secrets_path = Path.home() / ".delimit" / "secrets" / "reddit-proxy.json"
    if secrets_path.exists():
        try:
            secrets = json.loads(secrets_path.read_text())
            config["proxy_url"] = secrets.get("proxy_url", "")
        except Exception as e:
            logger.debug(f"Failed to load reddit-proxy secrets: {e}")
            
    return config

def fetch_subreddit(subreddit: str, sort: str = "new", limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch posts from a single subreddit with fallback chain.
    Returns standardized post dicts.
    """
    reddit_url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    
    # 1. Try Local Proxy (Residential IP)
    proxy_cfg = _get_proxy_config()
    proxy_url = proxy_cfg.get("proxy_url")
    if proxy_url:
        try:
            fetch_url = f"{proxy_url}?url={urllib.parse.quote(reddit_url, safe='')}"
            req = urllib.request.Request(fetch_url, headers={"User-Agent": "Delimit/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
                children = body.get("data", {}).get("children", [])
                return [c.get("data", {}) for c in children if c.get("data")]
        except Exception as e:
            logger.debug(f"Local proxy failed for r/{subreddit}: {e}")

    # 2. Fallback: PullPush API (Public Archive)
    try:
        pp_url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={subreddit}&size={limit}&sort=desc"
        req = urllib.request.Request(pp_url, headers={"User-Agent": "Delimit/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return body.get("data", [])
    except Exception as e:
        logger.debug(f"PullPush fallback failed for r/{subreddit}: {e}")

    # 3. Fallback: Direct (Often blocked on servers)
    try:
        req = urllib.request.Request(reddit_url, headers={"User-Agent": "Mozilla/5.0 (Delimit)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            children = body.get("data", {}).get("children", [])
            return [c.get("data", {}) for c in children if c.get("data")]
    except Exception as e:
        logger.warning(f"Direct fetch failed for r/{subreddit}: {e}")

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
            req = urllib.request.Request(fetch_url, headers={"User-Agent": "Delimit/1.0"})
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
