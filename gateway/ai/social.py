"""Automated social media — authentic engagement at scale.

Posts are value-first: tips, changelogs, governance insights.
Never spam, never generic marketing. Every post teaches something.

Multi-account support: credentials stored per handle in
~/.delimit/secrets/twitter-<handle>.json (e.g. twitter-delimit_ai.json).
Legacy twitter-full.json is treated as the default account.
"""
import json
import logging
import os
import random
import uuid
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("delimit.ai.social")

SECRETS_DIR = Path.home() / ".delimit" / "secrets"
SOCIAL_LOG = Path.home() / ".delimit" / "social_log.jsonl"
DRAFTS_FILE = Path.home() / ".delimit" / "social_drafts.jsonl"
ACCOUNTS_FILE = SECRETS_DIR / "twitter-accounts.json"

# Platform-specific tone guidelines for draft generation
# Used by AI agents when drafting content — included in draft metadata
# so the agent (or human reviewer) can adjust tone before posting.
PLATFORM_TONE = {
    "twitter": {
        "max_length": 280,
        "tone": "concise, technical, value-first",
        "rules": [
            "Lead with the insight, not the product",
            "One clear point per tweet",
            "Include a link or install command when relevant",
        ],
    },
    "reddit": {
        "max_length": 500,
        "tone": "casual, opinionated, human",
        "rules": [
            "NEVER use bullet points or numbered lists — instant AI tell",
            "Keep it to 2-3 sentences max, not multi-paragraph essays",
            "Write like you're typing on your phone, not writing a blog post",
            "Be opinionated — 'honestly X drives me nuts' not 'X is a known issue'",
            "Use contractions, sentence fragments, rough edges",
            "One sharp point per comment — don't cover three topics",
            "Personal experience framing — 'we hit this exact problem' not 'teams often encounter'",
            "A typo or two is fine — too polished = flagged as AI by mods",
            "NEVER structure responses with headers, bold text, or formatted lists",
            "r/devops mods actively flag LLM-generated content (learned 2026-03-27)",
        ],
    },
    "linkedin": {
        "max_length": 1300,
        "tone": "professional, concise, insight-driven",
        "rules": [
            "Lead with a hook question or surprising stat",
            "Keep paragraphs to 1-2 lines",
            "End with a clear CTA or question",
        ],
    },
}

# Content templates — each provides genuine value
CONTENT_TEMPLATES = {
    "tip": [
        "Tip: You can detect {count} types of breaking API changes with one line of YAML:\n\n- uses: delimit-ai/delimit-action@v1\n  with:\n    spec: api/openapi.yaml\n\nNo config needed. Advisory mode by default.",
        "Did you know? When you switch from Claude Code to Codex, you lose all context. With a shared ledger, say \"what's on the ledger?\" in any assistant and pick up exactly where you left off.",
        "API governance tip: The 3 most common breaking changes we catch:\n\n1. Endpoint removed without deprecation\n2. Required field added to request body\n3. Response field type changed\n\nAll detectable before merge.",
        "Quick tip: Run `npx delimit-cli doctor` in any project to check your governance setup. It checks for policies, specs, workflows, and git config in seconds.",
        "Pro tip: Use policy presets to match your team's risk tolerance:\n\n{bullet} strict — all violations are errors\n{bullet} default — balanced\n{bullet} relaxed — warnings only\n\n`npx delimit-cli init --preset strict`",
    ],
    "changelog": [
        "Just shipped: {feature}\n\n{detail}\n\nUpdate: npx delimit-cli@latest setup",
    ],
    "insight": [
        "We analyzed {count} API changes this week. {percent}% were breaking. The most common? {top_change}.\n\nAutomate this check: delimit.ai",
        "Hot take: In 2 years, unmanaged AI agents touching production code will be as unacceptable as unmanaged SSH keys.\n\nGovernance isn't optional. It's infrastructure.",
        "The problem with AI coding assistants isn't capability — it's context loss. Every time you switch models, you start from zero. That's the real productivity killer.",
    ],
    "engagement": [
        "What's the worst API breaking change you've shipped to production? We've seen some creative ones.",
        "How many AI coding assistants does your team use? We're seeing teams average 2-3, with context scattered across all of them.",
        "What's your API governance process today? Manual review? CI check? Nothing? (No judgment — that's why we built this.)",
    ],
}


def _resolve_creds_path(account: str = "") -> Path | None:
    """Resolve credentials file for a given account handle.

    Lookup order:
      1. ~/.delimit/secrets/twitter-<account>.json  (per-handle)
      2. ~/.delimit/secrets/twitter-full.json        (legacy default)
    """
    if account:
        per_handle = SECRETS_DIR / f"twitter-{account}.json"
        if per_handle.exists():
            return per_handle
    # Legacy fallback
    legacy = SECRETS_DIR / "twitter-full.json"
    if legacy.exists():
        return legacy
    return None


def get_twitter_client(account: str = ""):
    """Get authenticated Twitter client via tweepy for a specific account.

    Returns:
        Tuple of (client, handle, error). On success error is None.
        On failure client and handle are None and error is a non-empty string
        that distinguishes between "not configured" and "auth failed".

    Args:
        account: Twitter handle (without @). Empty string = default account.
    """
    acct_label = account or "default"
    creds_path = _resolve_creds_path(account)
    if not creds_path:
        configured = list_twitter_accounts()
        if configured:
            handles = [a["handle"] for a in configured]
            return None, None, (
                f"Account '{acct_label}' is not configured. "
                f"Configured accounts: {handles}. "
                f"Place credentials in ~/.delimit/secrets/twitter-{account}.json"
            )
        return None, None, (
            f"No Twitter accounts configured. "
            f"Place credentials in ~/.delimit/secrets/twitter-<handle>.json"
        )
    try:
        import tweepy
        creds = json.loads(creds_path.read_text())
        client = tweepy.Client(
            consumer_key=creds["consumer_key"],
            consumer_secret=creds["consumer_secret"],
            access_token=creds["access_token"],
            access_token_secret=creds["access_token_secret"],
        )
        handle = creds.get("handle", account or "delimit_ai")
        return client, handle, None
    except KeyError as e:
        msg = (
            f"Account '{acct_label}' is configured ({creds_path.name}) "
            f"but missing credential field {e}"
        )
        logger.error(msg)
        return None, None, msg
    except ImportError:
        msg = "tweepy is not installed. Run: pip install tweepy"
        logger.error(msg)
        return None, None, msg
    except json.JSONDecodeError as e:
        msg = (
            f"Account '{acct_label}' credentials file ({creds_path.name}) "
            f"contains invalid JSON: {e}"
        )
        logger.error(msg)
        return None, None, msg
    except Exception as e:
        msg = (
            f"Account '{acct_label}' is configured ({creds_path.name}) "
            f"but authentication failed: {e}"
        )
        logger.error(msg, exc_info=True)
        return None, None, msg


def list_twitter_accounts() -> list[dict]:
    """List all configured Twitter accounts, deduplicated by handle.

    When multiple credential files resolve to the same handle,
    the per-handle file (twitter-<handle>.json) wins over legacy files.
    """
    accounts = []
    seen_handles: set[str] = set()
    if not SECRETS_DIR.exists():
        return accounts
    for f in sorted(SECRETS_DIR.glob("twitter-*.json")):
        name = f.stem  # e.g. "twitter-delimit_ai"
        if name == "twitter-accounts":
            continue
        # Skip legacy twitter-full.json in this pass (handled below)
        if name == "twitter-full":
            continue
        try:
            creds = json.loads(f.read_text())
            handle = creds.get("handle", name.removeprefix("twitter-"))
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            accounts.append({"handle": handle, "file": f.name})
        except (json.JSONDecodeError, ValueError):
            pass
    # Include legacy twitter-full.json only if its handle is not already covered
    legacy = SECRETS_DIR / "twitter-full.json"
    if legacy.exists():
        try:
            creds = json.loads(legacy.read_text())
            handle = creds.get("handle", "default")
            if handle not in seen_handles:
                seen_handles.add(handle)
                accounts.append({"handle": handle, "file": "twitter-full.json", "default": True})
        except (json.JSONDecodeError, ValueError):
            pass
    return accounts


def post_tweet(text: str, account: str = "", quote_tweet_id: str = "",
               reply_to_id: str = "") -> dict:
    """Post a tweet via the Twitter API.

    Args:
        text: Tweet text content.
        account: Twitter handle (without @) to post from. Empty = default.
        quote_tweet_id: Tweet ID to quote. Creates a quote tweet.
        reply_to_id: Tweet ID to reply to. Creates a reply.
    """
    client, handle, init_error = get_twitter_client(account)
    if not client:
        # Always return the specific error from get_twitter_client.
        # Previous code fell through to a misleading "not found" message
        # when init_error was empty, even though the account was configured.
        if init_error:
            return {"error": init_error}
        # Fallback: should not be reachable, but be explicit
        return {"error": f"Failed to initialize Twitter client for account '{account or 'default'}'. "
                f"Check credentials in ~/.delimit/secrets/twitter-{account or 'full'}.json"}
    try:
        kwargs = {"text": text}
        if quote_tweet_id:
            kwargs["quote_tweet_id"] = quote_tweet_id
        if reply_to_id:
            kwargs["in_reply_to_tweet_id"] = reply_to_id
        result = client.create_tweet(**kwargs)
        tweet_id = result.data["id"]
        log_post("twitter", text, tweet_id, handle=handle,
                 quote_tweet_id=quote_tweet_id, reply_to_id=reply_to_id)
        return {
            "posted": True,
            "id": tweet_id,
            "handle": handle,
            "url": f"https://x.com/{handle}/status/{tweet_id}",
            "type": "quote_tweet" if quote_tweet_id else "reply" if reply_to_id else "tweet",
        }
    except Exception as e:
        return {"error": str(e), "handle": handle}


def generate_post(category: str = "", custom: str = "") -> dict:
    """Generate a post. If custom is provided, use that. Otherwise pick from templates."""
    if custom:
        return {"text": custom, "category": "custom"}

    if not category or category not in CONTENT_TEMPLATES:
        category = random.choice(list(CONTENT_TEMPLATES.keys()))

    templates = CONTENT_TEMPLATES[category]
    template = random.choice(templates)

    # Fill in template variables with realistic data
    text = template.format(
        count=27,
        percent=random.randint(15, 35),
        top_change=random.choice([
            "endpoint removed",
            "type changed",
            "required field added",
        ]),
        feature="(specify feature name)",
        detail="(specify feature details)",
        bullet="\u2022",
    )

    return {"text": text, "category": category}


def get_post_history(limit: int = 20) -> list:
    """Get recent post history from the JSONL log."""
    if not SOCIAL_LOG.exists():
        return []
    posts = []
    for line in reversed(SOCIAL_LOG.read_text().splitlines()):
        if not line.strip():
            continue
        try:
            posts.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass
        if len(posts) >= limit:
            break
    return posts


def log_post(platform: str, text: str, post_id: str = "", handle: str = "",
             quote_tweet_id: str = "", reply_to_id: str = ""):
    """Log a social media post to the JSONL log."""
    SOCIAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "handle": handle,
        "text": text[:200],
        "post_id": post_id,
    }
    if quote_tweet_id:
        entry["quote_tweet_id"] = quote_tweet_id
    if reply_to_id:
        entry["reply_to_id"] = reply_to_id
    with open(SOCIAL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def should_post_today() -> bool:
    """Check if we've hit the daily posting limit.

    Limit is configurable via DELIMIT_DAILY_TWEETS env var (default 8).
    Uses US Eastern Time for day boundaries since the posting schedule
    targets 9am/3pm ET.
    """
    from zoneinfo import ZoneInfo

    daily_limit = int(os.environ.get("DELIMIT_DAILY_TWEETS", "8"))
    et_now = datetime.now(ZoneInfo("America/New_York"))
    today_et = et_now.strftime("%Y-%m-%d")
    history = get_post_history(100)
    today_posts = []
    for p in history:
        ts_str = p.get("ts", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str).astimezone(ZoneInfo("America/New_York"))
                if ts.strftime("%Y-%m-%d") == today_et:
                    today_posts.append(p)
            except (ValueError, TypeError):
                continue
    return len(today_posts) < daily_limit


# ═════════════════════════════════════════════════════════════════════
#  DRAFT MODE — Queue content for review before posting
# ═════════════════════════════════════════════════════════════════════


def get_platform_tone(platform: str = "twitter") -> dict:
    """Return tone guidelines for a platform.

    AI agents should call this before drafting content to get
    platform-specific rules for voice, length, and formatting.
    """
    return PLATFORM_TONE.get(platform, PLATFORM_TONE.get("twitter", {}))


def save_draft(text: str, platform: str = "twitter", account: str = "",
               quote_tweet_id: str = "", reply_to_id: str = "") -> dict:
    """Save a social media post as a draft for later approval.

    Returns the draft entry with a unique draft_id and platform tone guidelines.
    """
    DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    draft_id = uuid.uuid4().hex[:12]
    tone = get_platform_tone(platform)
    entry = {
        "draft_id": draft_id,
        "text": text,
        "platform": platform,
        "account": account,
        "quote_tweet_id": quote_tweet_id,
        "reply_to_id": reply_to_id,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Check tone violations
    warnings = []
    if tone.get("max_length") and len(text) > tone["max_length"]:
        warnings.append(f"Text exceeds {platform} max length ({len(text)}/{tone['max_length']})")
    if platform == "reddit":
        if any(line.strip().startswith(("- ", "* ", "1.", "2.", "3.")) for line in text.split("\n")):
            warnings.append("REDDIT WARNING: Contains bullet/numbered lists — high risk of mod removal as AI content")
        if text.count("\n\n") >= 3:
            warnings.append("REDDIT WARNING: Multi-paragraph essay format — shorten to 2-3 sentences")
        if "**" in text:
            warnings.append("REDDIT WARNING: Contains bold formatting — too polished for Reddit")
    if warnings:
        entry["tone_warnings"] = warnings
    with open(DRAFTS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def store_draft_message_id(draft_id: str, message_id: str) -> bool:
    """Store the outbound notification Message-ID on a draft record.

    This enables In-Reply-To header matching for auto-approval via the
    inbox polling daemon (Consensus 116).

    Args:
        draft_id: The 12-char hex draft ID.
        message_id: The Message-ID header from the sent notification email.

    Returns:
        True if the draft was found and updated, False otherwise.
    """
    all_entries = _load_all_drafts()
    for entry in all_entries:
        if entry.get("draft_id") == draft_id:
            entry["notification_message_id"] = message_id
            _rewrite_drafts(all_entries)
            return True
    return False


def list_drafts(status: str = "pending") -> list[dict]:
    """List drafts filtered by status (pending, approved, rejected)."""
    if not DRAFTS_FILE.exists():
        return []
    drafts = []
    for line in DRAFTS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("status") == status:
                drafts.append(entry)
        except (json.JSONDecodeError, ValueError):
            pass
    return drafts


def _rewrite_drafts(all_entries: list[dict]) -> None:
    """Rewrite the drafts file with updated entries."""
    DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DRAFTS_FILE, "w") as f:
        for entry in all_entries:
            f.write(json.dumps(entry) + "\n")


def _load_all_drafts() -> list[dict]:
    """Load all draft entries from the JSONL file."""
    if not DRAFTS_FILE.exists():
        return []
    entries = []
    for line in DRAFTS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass
    return entries


def approve_draft(draft_id: str) -> dict:
    """Approve a draft and post it. Returns the post result."""
    all_entries = _load_all_drafts()
    target = None
    for entry in all_entries:
        if entry.get("draft_id") == draft_id:
            target = entry
            break
    if not target:
        return {"error": f"Draft '{draft_id}' not found"}
    if target.get("status") != "pending":
        return {"error": f"Draft '{draft_id}' is already {target.get('status')}"}

    # Post it
    result = post_tweet(
        target["text"],
        account=target.get("account", ""),
        quote_tweet_id=target.get("quote_tweet_id", ""),
        reply_to_id=target.get("reply_to_id", ""),
    )

    if "error" in result:
        return result

    # Update status
    target["status"] = "approved"
    target["approved_at"] = datetime.now(timezone.utc).isoformat()
    target["post_result"] = result
    _rewrite_drafts(all_entries)
    return {"draft_id": draft_id, "status": "approved", "post_result": result}


def reject_draft(draft_id: str) -> dict:
    """Reject a draft. It will not be posted."""
    all_entries = _load_all_drafts()
    target = None
    for entry in all_entries:
        if entry.get("draft_id") == draft_id:
            target = entry
            break
    if not target:
        return {"error": f"Draft '{draft_id}' not found"}
    if target.get("status") != "pending":
        return {"error": f"Draft '{draft_id}' is already {target.get('status')}"}

    target["status"] = "rejected"
    target["rejected_at"] = datetime.now(timezone.utc).isoformat()
    _rewrite_drafts(all_entries)
    return {"draft_id": draft_id, "status": "rejected"}
