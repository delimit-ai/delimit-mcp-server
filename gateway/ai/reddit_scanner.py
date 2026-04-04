"""Reddit bulk scanner -- fetch, categorize, and rank posts for outreach.

Scans 25+ subreddits via the residential proxy, scores each post on
engagement, freshness, comment opportunity, and venture relevance,
then returns a ranked list of outreach targets.

Rate limited to 1 request per 2 seconds to stay well under Reddit limits.
Results are persisted to ~/.delimit/reddit_scans/{date}.json for dedup.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.reddit_scanner")

# ---------------------------------------------------------------------------
#  Subreddit groups
# ---------------------------------------------------------------------------

SCAN_GROUPS: Dict[str, List[str]] = {
    "delimit_core": ["ClaudeAI", "vibecoding", "cursor", "AI_Agents"],
    "delimit_adjacent": ["devops", "programming", "ContextEngineering", "LocalLLaMA", "MachineLearning"],
    "domainvested": ["Domains", "Entrepreneur", "SideProject", "flipping"],
    "wirereport": ["sportsbook", "sportsbetting"],
    "stakeone": ["harmony_one", "CryptoCurrency", "defi"],
    "karma_building": ["SaaS", "opensource", "webdev", "startups", "ExperiencedDevs", "selfhosted", "IndieHackers"],
}

ALL_SUBREDDITS: List[str] = [sub for subs in SCAN_GROUPS.values() for sub in subs]

# Reverse lookup: subreddit -> group
_SUB_TO_GROUP: Dict[str, str] = {}
for _group, _subs in SCAN_GROUPS.items():
    for _sub in _subs:
        _SUB_TO_GROUP[_sub.lower()] = _group

# ---------------------------------------------------------------------------
#  Venture keywords (loaded from disk or defined inline as fallback)
# ---------------------------------------------------------------------------

_VENTURE_KEYWORDS_FALLBACK: Dict[str, List[str]] = {
    "delimit": [
        "api governance", "breaking changes", "openapi", "api linting",
        "mcp server", "mcp tools", "claude.md", "claude code",
        "ai coding", "vibe coding", "semver", "api compatibility",
        "schema migration", "api versioning", "contract testing",
        "session handoff", "agent state", "context engineering",
    ],
    "domainvested": [
        "domain investing", "domain appraisal", "domain flipping",
        "expired domains", "brandable domains", "domain valuation",
        "namepros", "domain name",
    ],
    "wirereport": [
        "sports api", "live sports data", "sports scores",
        "sports news automation", "sports betting api",
    ],
    "stakeone": [
        "harmony one", "harmony validator", "one staking",
        "harmony blockchain", "harmony network",
    ],
}

# ---------------------------------------------------------------------------
#  Pain point categories for product intelligence
# ---------------------------------------------------------------------------

PAIN_CATEGORIES: Dict[str, List[str]] = {
    "context_loss": ["lost context", "re-explain", "starting from zero", "forgot", "doesn't remember"],
    "rate_limits": ["rate limit", "session limit", "throttled", "burned through", "ran out"],
    "multi_model": ["switching between", "codex and claude", "multiple models", "different tool"],
    "code_quality": ["broke my", "deleted", "undid", "regression", "broke production"],
    "session_management": ["session died", "context window", "compact", "handoff"],
    "governance": ["breaking change", "API broke", "schema", "backward compat"],
    "onboarding": ["how to start", "getting started", "setup", "configure"],
    "cost": ["expensive", "pricing", "cost", "$200", "billing"],
}

# Which pain categories map to Delimit features
_PAIN_TO_RELEVANCE: Dict[str, str] = {
    "context_loss": "existing_feature",       # persistent context / session handoff
    "session_management": "existing_feature",  # session handoff, compact
    "governance": "existing_feature",          # API governance, breaking change detection
    "multi_model": "existing_feature",         # cross-model continuity
    "code_quality": "planned_feature",         # test verification, guardrails
    "onboarding": "planned_feature",           # delimit init, doctor, setup
    "rate_limits": "new_opportunity",          # not directly addressed yet
    "cost": "new_opportunity",                 # pricing transparency / cost tracking
}

PROXY_URL = "http://127.0.0.1:4819/reddit-fetch"
SCANS_DIR = Path.home() / ".delimit" / "reddit_scans"
VENTURES_CONFIG_PATH = Path.home() / ".delimit" / "social_target_ventures.json"

# Posts by these authors are always skipped
SKIP_AUTHORS = {"delimitdev", "delimit_ai", "AutoModerator", "[deleted]"}

# ---------------------------------------------------------------------------
#  Keyword loading
# ---------------------------------------------------------------------------


def _load_venture_keywords() -> Dict[str, List[str]]:
    """Load venture keywords from disk config, falling back to built-in list."""
    if VENTURES_CONFIG_PATH.exists():
        try:
            data = json.loads(VENTURES_CONFIG_PATH.read_text())
            ventures = data.get("ventures", {})
            result: Dict[str, List[str]] = {}
            for name, cfg in ventures.items():
                topics = cfg.get("topics", [])
                if topics:
                    result[name] = [t.lower() for t in topics]
            if result:
                return result
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load venture keywords: %s", exc)
    return {k: [t.lower() for t in v] for k, v in _VENTURE_KEYWORDS_FALLBACK.items()}


# ---------------------------------------------------------------------------
#  Fetching
# ---------------------------------------------------------------------------


def _fetch_subreddit(
    subreddit: str,
    sort: str = "hot",
    limit: int = 10,
    *,
    proxy_url: str = PROXY_URL,
) -> List[Dict[str, Any]]:
    """Fetch posts from a single subreddit via the residential proxy.

    The proxy endpoint expects a query parameter ``url`` containing the
    actual Reddit JSON URL.  Returns a list of extracted post dicts.
    """
    reddit_url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    fetch_url = f"{proxy_url}?url={urllib.request.quote(reddit_url, safe='')}"

    req = urllib.request.Request(
        fetch_url,
        headers={"User-Agent": "delimit-scanner/1.0", "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Failed to fetch r/%s: %s", subreddit, exc)
        return []

    # Reddit returns {"data": {"children": [...]}}
    children = []
    if isinstance(body, dict):
        children = body.get("data", {}).get("children", [])

    posts: List[Dict[str, Any]] = []
    for child in children:
        d = child.get("data", {})
        if not d:
            continue
        # Skip stickied
        if d.get("stickied"):
            continue
        # Skip our own posts
        author = d.get("author", "")
        if author in SKIP_AUTHORS:
            continue

        posts.append({
            "id": d.get("id", ""),
            "title": d.get("title", ""),
            "author": author,
            "score": d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "subreddit": d.get("subreddit", subreddit),
            "permalink": d.get("permalink", ""),
            "selftext": (d.get("selftext") or "")[:200],
            "created_utc": d.get("created_utc", 0),
        })

    return posts


def fetch_all(
    limit_per_sub: int = 10,
    sort: str = "hot",
    *,
    rate_limit: float = 4.0,
    proxy_url: str = PROXY_URL,
    subreddits: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch posts from all configured subreddits.

    Returns the combined flat list of post dicts. Inserts a ``group``
    field into each post based on which scan group the subreddit belongs to.
    """
    targets = subreddits or ALL_SUBREDDITS
    all_posts: List[Dict[str, Any]] = []

    import random
    for i, sub in enumerate(targets):
        if i > 0 and rate_limit > 0:
            # Add jitter to avoid bot-pattern detection
            time.sleep(rate_limit + random.uniform(0, 2.0))
        posts = _fetch_subreddit(sub, sort=sort, limit=limit_per_sub, proxy_url=proxy_url)
        group = _SUB_TO_GROUP.get(sub.lower(), "unknown")
        for p in posts:
            p["group"] = group
        all_posts.extend(posts)
        logger.info("Fetched %d posts from r/%s (%s)", len(posts), sub, group)

    return all_posts


# ---------------------------------------------------------------------------
#  Scoring & classification
# ---------------------------------------------------------------------------


def _age_hours(created_utc: float, now: Optional[float] = None) -> float:
    """Return how many hours old a post is."""
    now_ts = now or time.time()
    return max(0.0, (now_ts - created_utc) / 3600.0)


def _freshness_multiplier(age_h: float) -> float:
    """Return freshness multiplier: <6h = 2x, <12h = 1.5x, else 1x."""
    if age_h < 6:
        return 2.0
    if age_h < 12:
        return 1.5
    return 1.0


def _relevance_tags(title: str, selftext: str, venture_keywords: Dict[str, List[str]]) -> List[str]:
    """Return list of matching keyword tags from the post text."""
    combined = (title + " " + selftext).lower()
    tags: List[str] = []
    for _venture, keywords in venture_keywords.items():
        for kw in keywords:
            if kw in combined and kw not in tags:
                tags.append(kw)
    return tags


def _suggest_angle(relevance_tags: List[str], group: str) -> str:
    """Generate a brief suggested engagement angle."""
    if not relevance_tags:
        if group == "karma_building":
            return "general expertise comment for karma building"
        return "tangentially relevant -- low priority"

    tag_str = ", ".join(relevance_tags[:3])

    angle_map = {
        "delimit_core": f"expert comment on {tag_str}",
        "delimit_adjacent": f"helpful technical reply mentioning {tag_str}",
        "domainvested": f"domain industry insight on {tag_str}",
        "wirereport": f"sports data perspective on {tag_str}",
        "stakeone": f"validator/staking expertise on {tag_str}",
        "karma_building": f"genuine helpful comment touching on {tag_str}",
    }
    return angle_map.get(group, f"engage on {tag_str}")


def extract_pain_points(title: str, selftext: str) -> Dict[str, Any]:
    """Extract actionable product insights from a post's text.

    Returns a dict with:
        pain_point:            one-sentence description of the user's problem
        delimit_relevance:     existing_feature | planned_feature | new_opportunity | not_relevant
        suggested_ledger_item: one-line ledger title (empty string if not relevant)
        product_insight:       one-sentence takeaway about user needs
        matched_categories:    list of PAIN_CATEGORIES keys that matched
    """
    combined = (title + " " + selftext).lower()

    matched_cats: List[str] = []
    matched_phrases: List[str] = []
    for category, phrases in PAIN_CATEGORIES.items():
        for phrase in phrases:
            if phrase in combined:
                if category not in matched_cats:
                    matched_cats.append(category)
                matched_phrases.append(phrase)

    if not matched_cats:
        return {
            "pain_point": "",
            "delimit_relevance": "not_relevant",
            "suggested_ledger_item": "",
            "product_insight": "",
            "matched_categories": [],
        }

    # Determine overall relevance from the most relevant category
    relevance_priority = ["existing_feature", "planned_feature", "new_opportunity"]
    best_relevance = "not_relevant"
    for cat in matched_cats:
        cat_rel = _PAIN_TO_RELEVANCE.get(cat, "not_relevant")
        if cat_rel in relevance_priority:
            idx = relevance_priority.index(cat_rel)
            best_idx = relevance_priority.index(best_relevance) if best_relevance in relevance_priority else len(relevance_priority)
            if idx < best_idx:
                best_relevance = cat_rel

    # Build pain_point: summarize from title (truncated, cleaned)
    pain_point = title.strip()
    if len(pain_point) > 120:
        pain_point = pain_point[:117] + "..."

    # Build suggested ledger item from category + title
    cat_labels = {
        "context_loss": "Context persistence",
        "rate_limits": "Rate limit mitigation",
        "multi_model": "Multi-model workflow",
        "code_quality": "Code safety guardrail",
        "session_management": "Session management",
        "governance": "API governance",
        "onboarding": "Onboarding flow",
        "cost": "Cost management",
    }
    primary_cat = matched_cats[0]
    ledger_prefix = cat_labels.get(primary_cat, primary_cat.replace("_", " ").title())

    # Extract a compact actionable phrase from the title
    ledger_item = ""
    if best_relevance != "not_relevant":
        # Use the first 80 chars of the title as the action item basis
        short_title = title.strip()[:80].rstrip(".")
        ledger_item = f"{ledger_prefix}: {short_title}"

    # Build product insight
    cat_insights = {
        "context_loss": "Users lose productivity when context does not persist across sessions",
        "rate_limits": "Rate limits and session caps are a recurring friction point for power users",
        "multi_model": "Users want to move between AI tools without rebuilding context",
        "code_quality": "Users fear AI making destructive changes without guardrails",
        "session_management": "Session lifecycle management is a top concern for daily AI users",
        "governance": "Teams need automated detection of breaking changes in APIs",
        "onboarding": "New users struggle with initial setup and configuration",
        "cost": "Cost predictability and transparency matter to individual developers",
    }
    insight = cat_insights.get(primary_cat, f"Users express frustration with {primary_cat.replace('_', ' ')}")

    return {
        "pain_point": pain_point,
        "delimit_relevance": best_relevance,
        "suggested_ledger_item": ledger_item,
        "product_insight": insight,
        "matched_categories": matched_cats,
    }


def _build_product_insights(scored_posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate pain_points across all scored posts into a product insights summary.

    Returns:
        top_pain_points:              most mentioned pain categories with counts
        new_opportunities:            suggested ledger items for unaddressed pain
        existing_feature_validation:  posts that validate features we already ship
    """
    from collections import Counter

    cat_counter: Counter = Counter()
    new_opps: List[Dict[str, str]] = []
    existing_validations: List[Dict[str, str]] = []

    for post in scored_posts:
        pp = post.get("pain_points")
        if not pp or not pp.get("matched_categories"):
            continue

        for cat in pp["matched_categories"]:
            cat_counter[cat] += 1

        relevance = pp.get("delimit_relevance", "not_relevant")
        entry = {
            "title": post.get("title", ""),
            "subreddit": post.get("subreddit", ""),
            "url": post.get("url", ""),
            "pain_point": pp.get("pain_point", ""),
            "suggested_ledger_item": pp.get("suggested_ledger_item", ""),
        }

        if relevance == "new_opportunity" and pp.get("suggested_ledger_item"):
            new_opps.append(entry)
        elif relevance == "existing_feature":
            existing_validations.append(entry)

    # Sort pain points by frequency
    top_pains = [
        {"category": cat, "count": count}
        for cat, count in cat_counter.most_common(10)
    ]

    return {
        "top_pain_points": top_pains,
        "new_opportunities": new_opps[:20],
        "existing_feature_validation": existing_validations[:20],
    }


def score_and_classify(
    posts: List[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    venture_keywords: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Score and classify posts, returning them sorted by rank (best first).

    Each post dict is augmented with:
        engagement_score, age_hours, freshness_mult, relevance_tags,
        karma_building, suggested_angle, priority, final_score, pain_points
    """
    kw = venture_keywords or _load_venture_keywords()
    now_ts = now or time.time()
    scored: List[Dict[str, Any]] = []

    for post in posts:
        score = post.get("score", 0)
        comments = post.get("num_comments", 0)
        created = post.get("created_utc", 0)
        group = post.get("group", "unknown")

        age_h = _age_hours(created, now_ts)
        engagement = score * 0.4 + comments * 0.6
        fresh_mult = _freshness_multiplier(age_h)

        # Comment opportunity bonus: high engagement but room to comment
        comment_opp = 1.0
        if engagement > 5 and comments < 30:
            comment_opp = 1.3

        tags = _relevance_tags(
            post.get("title", ""),
            post.get("selftext", ""),
            kw,
        )
        relevance_mult = 1.0 + 0.2 * min(len(tags), 5)  # up to 2.0x

        final_score = engagement * fresh_mult * comment_opp * relevance_mult
        is_karma = group == "karma_building"

        # Classification
        if post.get("stickied") or age_h > 48 or comments > 100:
            priority = "skip"
        elif final_score >= 30 and age_h < 12 and comments < 50:
            priority = "high_priority"
        elif final_score >= 10 or (len(tags) >= 2 and age_h < 24):
            priority = "medium_priority"
        elif final_score >= 3:
            priority = "low_priority"
        else:
            priority = "skip"

        angle = _suggest_angle(tags, group)
        pain = extract_pain_points(post.get("title", ""), post.get("selftext", ""))

        scored.append({
            **post,
            "engagement_score": round(engagement, 1),
            "age_hours": round(age_h, 1),
            "freshness_mult": fresh_mult,
            "relevance_tags": tags,
            "karma_building": is_karma,
            "suggested_angle": angle,
            "priority": priority,
            "final_score": round(final_score, 2),
            "url": f"https://reddit.com{post.get('permalink', '')}",
            "pain_points": pain,
        })

    # Sort by final_score descending
    scored.sort(key=lambda x: x["final_score"], reverse=True)

    # Assign ranks
    for i, item in enumerate(scored):
        item["rank"] = i + 1

    return scored


def _save_scan(result: Dict[str, Any], scan_time: datetime) -> Path:
    """Save scan results to ~/.delimit/reddit_scans/{date}.json."""
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    filename = scan_time.strftime("%Y-%m-%dT%H%M%S") + ".json"
    path = SCANS_DIR / filename
    path.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Scan saved to %s", path)
    return path


# ---------------------------------------------------------------------------
#  Main scan orchestrator
# ---------------------------------------------------------------------------


def scan_all(
    limit_per_sub: int = 10,
    sort: str = "hot",
    *,
    rate_limit: float = 4.0,
    proxy_url: str = PROXY_URL,
) -> Dict[str, Any]:
    """Full scan: fetch all subreddits, score, classify, persist.

    Returns the complete result dict with targets, stats, and grouping.
    """
    scan_start = datetime.now(timezone.utc)

    raw_posts = fetch_all(
        limit_per_sub=limit_per_sub,
        sort=sort,
        rate_limit=rate_limit,
        proxy_url=proxy_url,
    )

    scored = score_and_classify(raw_posts)

    # Group by scan group
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for item in scored:
        g = item.get("group", "unknown")
        by_group.setdefault(g, []).append(item)

    # Stats
    stats: Dict[str, int] = {"high_priority": 0, "medium_priority": 0, "low_priority": 0, "skip": 0}
    for item in scored:
        p = item.get("priority", "skip")
        stats[p] = stats.get(p, 0) + 1

    # Only include non-skip targets in the top-level targets list
    targets = [t for t in scored if t["priority"] != "skip"]

    # Product intelligence summary
    product_insights = _build_product_insights(scored)

    result: Dict[str, Any] = {
        "scanned_at": scan_start.isoformat(),
        "total_posts": len(raw_posts),
        "subreddits_scanned": len(set(p.get("subreddit", "") for p in raw_posts)),
        "targets": targets,
        "by_group": by_group,
        "stats": stats,
        "product_insights": product_insights,
    }

    # Persist to disk
    _save_scan(result, scan_start)

    return result
