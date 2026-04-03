"""GitHub scanner -- pulse health checks and hunter lead discovery.

Pulse (every 10 min): own-repo stars, forks, issues, traffic, referrers.
Hunter (hourly): competitor action users, adoption leads, pain threads.
Deep (daily): stub for ecosystem intel and pain clustering.

All GitHub API calls use ``gh api`` via subprocess (already authenticated).
Rate limited to 2 seconds between search API calls (30/min limit).
Results persisted to ~/.delimit/github_scans/{date}_{cadence}.json.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.github_scanner")

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

OWN_REPOS = [
    "delimit-ai/delimit-mcp-server",
    "delimit-ai/delimit-action",
    "delimit-ai/delimit-quickstart",
]

INTERNAL_USERS = {"infracore", "crypttrx"}

COMPETITOR_ACTIONS = [
    "tufin/oasdiff-action",
    "stoplightio/spectral-action",
    "redocly/openapi-cli",
    "opticdev/optic",
]

SCANS_DIR = Path.home() / ".delimit" / "github_scans"
KNOWN_FILE = Path.home() / ".delimit" / "github_known.json"

# Rate limit: 2 seconds between search queries
SEARCH_RATE_LIMIT = 2.0

# ---------------------------------------------------------------------------
#  Pain categories (extend reddit_scanner's taxonomy)
# ---------------------------------------------------------------------------

# Import base pain categories from reddit scanner, with fallback
try:
    from ai.reddit_scanner import PAIN_CATEGORIES as _BASE_PAIN
except ImportError:
    _BASE_PAIN = {
        "context_loss": ["lost context", "re-explain", "starting from zero", "forgot", "doesn't remember"],
        "rate_limits": ["rate limit", "session limit", "throttled", "burned through", "ran out"],
        "multi_model": ["switching between", "codex and claude", "multiple models", "different tool"],
        "code_quality": ["broke my", "deleted", "undid", "regression", "broke production"],
        "session_management": ["session died", "context window", "compact", "handoff"],
        "governance": ["breaking change", "API broke", "schema", "backward compat"],
        "onboarding": ["how to start", "getting started", "setup", "configure"],
        "cost": ["expensive", "pricing", "cost", "$200", "billing"],
    }

GITHUB_PAIN_CATEGORIES: Dict[str, List[str]] = {
    **_BASE_PAIN,
    "breaking_changes": ["broke our clients", "backward compatibility", "breaking change", "api contract"],
    "schema_drift": ["schema drift", "spec out of sync", "generated client broke", "stale openapi"],
    "ci_governance": ["no gate", "merged without review", "api review process", "caught in production"],
    "monorepo": ["multiple specs", "monorepo", "workspace openapi"],
}

# Which pain categories map to Delimit features
_PAIN_TO_RELEVANCE: Dict[str, str] = {
    "context_loss": "existing_feature",
    "session_management": "existing_feature",
    "governance": "existing_feature",
    "multi_model": "existing_feature",
    "breaking_changes": "existing_feature",
    "schema_drift": "existing_feature",
    "ci_governance": "existing_feature",
    "code_quality": "planned_feature",
    "onboarding": "planned_feature",
    "monorepo": "planned_feature",
    "rate_limits": "new_opportunity",
    "cost": "new_opportunity",
}


# ---------------------------------------------------------------------------
#  GitHub API helpers
# ---------------------------------------------------------------------------


def _gh_api(endpoint: str, *, accept: str = "") -> Optional[Dict[str, Any]]:
    """Call ``gh api <endpoint>`` and return parsed JSON, or None on error."""
    cmd = ["gh", "api", endpoint]
    if accept:
        cmd.extend(["-H", f"Accept: {accept}"])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("gh api %s failed: %s", endpoint, result.stderr.strip()[:200])
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("gh api %s error: %s", endpoint, exc)
        return None


def _gh_search(query: str, endpoint: str = "search/code") -> Optional[Dict[str, Any]]:
    """Run a GitHub search API query with rate limiting.

    Sleeps SEARCH_RATE_LIMIT seconds before each call to stay within
    the 30 requests/minute search API limit.
    """
    time.sleep(SEARCH_RATE_LIMIT)
    return _gh_api(f"{endpoint}?{query}")


# ---------------------------------------------------------------------------
#  Known state management (stargazers, forkers)
# ---------------------------------------------------------------------------


def _load_known() -> Dict[str, Any]:
    """Load known stargazers/forkers from disk."""
    if KNOWN_FILE.exists():
        try:
            return json.loads(KNOWN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"stargazers": {}, "forkers": {}, "last_updated": None}


def _save_known(data: Dict[str, Any]) -> None:
    """Save known stargazers/forkers to disk."""
    KNOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    KNOWN_FILE.write_text(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
#  Scoring
# ---------------------------------------------------------------------------


def _score_finding(
    category: str,
    stars: int = 0,
    recency_days: float = 999,
    relevance_signals: int = 0,
) -> int:
    """Score a finding 0-100 based on stars, recency, and relevance.

    Category weights:
      competitor_user: high base (they already believe in governance)
      adoption_lead:   medium base
      pain_thread:     weighted by recency and reactions
      own_repo_activity: always 50 (informational)
      competitive_move: high base
    """
    base_scores = {
        "competitor_user": 60,
        "adoption_lead": 40,
        "pain_thread": 30,
        "own_repo_activity": 50,
        "competitive_move": 70,
    }
    score = base_scores.get(category, 30)

    # Star bonus: +1 per 100 stars, max +20
    score += min(20, stars // 100)

    # Recency bonus: +20 for <1 day, +10 for <7 days
    if recency_days < 1:
        score += 20
    elif recency_days < 7:
        score += 10
    elif recency_days < 30:
        score += 5

    # Relevance signal bonus: +5 per signal, max +15
    score += min(15, relevance_signals * 5)

    return min(100, max(0, score))


# ---------------------------------------------------------------------------
#  Pain-point extraction
# ---------------------------------------------------------------------------


def extract_pain_points(title: str, body: str = "") -> Dict[str, Any]:
    """Extract pain categories from an issue title and body.

    Returns dict with matched_categories, relevance, and pain_tags.
    """
    combined = (title + " " + body).lower()

    matched_cats: List[str] = []
    pain_tags: List[str] = []
    for category, phrases in GITHUB_PAIN_CATEGORIES.items():
        for phrase in phrases:
            if phrase in combined:
                if category not in matched_cats:
                    matched_cats.append(category)
                if phrase not in pain_tags:
                    pain_tags.append(phrase)

    if not matched_cats:
        return {
            "matched_categories": [],
            "pain_tags": [],
            "delimit_relevance": "not_relevant",
        }

    # Best relevance
    relevance_priority = ["existing_feature", "planned_feature", "new_opportunity"]
    best_relevance = "not_relevant"
    for cat in matched_cats:
        cat_rel = _PAIN_TO_RELEVANCE.get(cat, "not_relevant")
        if cat_rel in relevance_priority:
            idx = relevance_priority.index(cat_rel)
            best_idx = (
                relevance_priority.index(best_relevance)
                if best_relevance in relevance_priority
                else len(relevance_priority)
            )
            if idx < best_idx:
                best_relevance = cat_rel

    return {
        "matched_categories": matched_cats,
        "pain_tags": pain_tags,
        "delimit_relevance": best_relevance,
    }


# ---------------------------------------------------------------------------
#  Pulse module: own-repo health
# ---------------------------------------------------------------------------


def pulse_scan() -> Dict[str, Any]:
    """Own-repo health check: stars, forks, issues, traffic, referrers.

    Detects new stargazers and forkers by comparing against known state.
    """
    known = _load_known()
    repos_data: List[Dict[str, Any]] = []

    for repo in OWN_REPOS:
        repo_info = _gh_api(f"repos/{repo}")
        if not repo_info:
            repos_data.append({"repo": repo, "error": "failed to fetch"})
            continue

        stars = repo_info.get("stargazers_count", 0)
        forks = repo_info.get("forks_count", 0)

        # Fetch stargazers for delta detection
        stargazers_data = _gh_api(
            f"repos/{repo}/stargazers?per_page=100",
            accept="application/vnd.github.star+json",
        )
        current_stargazers = set()
        if isinstance(stargazers_data, list):
            for sg in stargazers_data:
                user = sg.get("user", {}).get("login", "")
                if user:
                    current_stargazers.add(user)

        known_stargazers = set(known.get("stargazers", {}).get(repo, []))
        new_stargazers = current_stargazers - known_stargazers

        # Fetch forkers for delta detection
        forks_data = _gh_api(f"repos/{repo}/forks?sort=newest&per_page=50")
        current_forkers = set()
        if isinstance(forks_data, list):
            for fork in forks_data:
                owner = fork.get("owner", {}).get("login", "")
                if owner:
                    current_forkers.add(owner)

        known_forkers = set(known.get("forkers", {}).get(repo, []))
        new_forkers = current_forkers - known_forkers

        # Fetch external issues/PRs (not from internal users)
        issues_data = _gh_api(
            f"repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=20"
        )
        external_issues: List[Dict[str, str]] = []
        if isinstance(issues_data, list):
            for issue in issues_data:
                author = issue.get("user", {}).get("login", "")
                if author and author not in INTERNAL_USERS:
                    external_issues.append({
                        "number": issue.get("number", 0),
                        "title": issue.get("title", ""),
                        "author": author,
                        "url": issue.get("html_url", ""),
                        "is_pr": "pull_request" in issue,
                    })

        # Clone traffic
        traffic = _gh_api(f"repos/{repo}/traffic/clones")
        clone_count = 0
        clone_uniques = 0
        if traffic:
            clone_count = traffic.get("count", 0)
            clone_uniques = traffic.get("uniques", 0)

        # Referrers
        referrers_data = _gh_api(f"repos/{repo}/traffic/popular/referrers")
        referrers: List[Dict[str, Any]] = []
        if isinstance(referrers_data, list):
            for ref in referrers_data:
                referrers.append({
                    "referrer": ref.get("referrer", ""),
                    "count": ref.get("count", 0),
                    "uniques": ref.get("uniques", 0),
                })

        # Update known state
        if current_stargazers:
            known.setdefault("stargazers", {})[repo] = sorted(current_stargazers)
        if current_forkers:
            known.setdefault("forkers", {})[repo] = sorted(current_forkers)

        repos_data.append({
            "repo": repo,
            "stars": stars,
            "forks": forks,
            "new_stargazers": sorted(new_stargazers),
            "new_forkers": sorted(new_forkers),
            "external_issues": external_issues,
            "clones_14d": clone_count,
            "clone_uniques_14d": clone_uniques,
            "referrers": referrers,
            "score": _score_finding(
                "own_repo_activity",
                stars=stars,
                relevance_signals=len(new_stargazers) + len(new_forkers) + len(external_issues),
            ),
            "category": "own_repo_activity",
        })

    _save_known(known)

    return {
        "cadence": "pulse",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "repos": repos_data,
    }


# ---------------------------------------------------------------------------
#  Hunter module: competitor users, adoption leads, pain threads
# ---------------------------------------------------------------------------


def _search_competitor_users(limit: int = 20) -> List[Dict[str, Any]]:
    """Find repos using competitor GitHub Actions in their workflows."""
    findings: List[Dict[str, Any]] = []
    seen_repos = set()

    for action in COMPETITOR_ACTIONS:
        query = f"q=uses:+{action}+path:.github/workflows&per_page={min(limit, 30)}"
        data = _gh_search(query, endpoint="search/code")
        if not data:
            continue

        items = data.get("items", [])
        for item in items:
            repo_info = item.get("repository", {})
            full_name = repo_info.get("full_name", "")
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            stars = repo_info.get("stargazers_count", 0)
            description = repo_info.get("description") or ""

            findings.append({
                "repo": full_name,
                "stars": stars,
                "description": description[:200],
                "competitor_action": action,
                "workflow_file": item.get("path", ""),
                "url": repo_info.get("html_url", ""),
                "category": "competitor_user",
                "score": _score_finding(
                    "competitor_user",
                    stars=stars,
                    relevance_signals=1,
                ),
                "auto_ledger": False,  # set below
            })

    # Mark high-score findings for auto-ledger
    for f in findings:
        if f["score"] >= 75:
            f["auto_ledger"] = True

    # Sort by score descending
    findings.sort(key=lambda x: x["score"], reverse=True)
    return findings[:limit]


def _search_adoption_leads(limit: int = 20) -> List[Dict[str, Any]]:
    """Find repos with OpenAPI specs and repos using MCP + Claude."""
    findings: List[Dict[str, Any]] = []
    seen_repos = set()

    # Repos with OpenAPI specs
    query = f"q=openapi+path:api+extension:yaml&per_page={min(limit, 20)}"
    data = _gh_search(query, endpoint="search/code")
    if data:
        for item in data.get("items", []):
            repo_info = item.get("repository", {})
            full_name = repo_info.get("full_name", "")
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            stars = repo_info.get("stargazers_count", 0)
            findings.append({
                "repo": full_name,
                "stars": stars,
                "description": (repo_info.get("description") or "")[:200],
                "spec_path": item.get("path", ""),
                "url": repo_info.get("html_url", ""),
                "category": "adoption_lead",
                "subcategory": "openapi_spec",
                "score": _score_finding("adoption_lead", stars=stars),
            })

    # Repos mentioning MCP + claude
    query2 = f"q=MCP+server+claude+code&sort=updated&per_page={min(limit, 20)}"
    data2 = _gh_search(query2, endpoint="search/repositories")
    if data2:
        for item in data2.get("items", []):
            full_name = item.get("full_name", "")
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            stars = item.get("stargazers_count", 0)
            findings.append({
                "repo": full_name,
                "stars": stars,
                "description": (item.get("description") or "")[:200],
                "url": item.get("html_url", ""),
                "category": "adoption_lead",
                "subcategory": "mcp_ecosystem",
                "score": _score_finding("adoption_lead", stars=stars, relevance_signals=2),
            })

    findings.sort(key=lambda x: x["score"], reverse=True)
    return findings[:limit]


def _search_pain_threads(limit: int = 20) -> List[Dict[str, Any]]:
    """Find open issues mentioning breaking changes and API pain."""
    findings: List[Dict[str, Any]] = []

    queries = [
        "q=breaking+changes+API+is:open&sort=created",
        "q=schema+drift+openapi+is:open&sort=created",
        "q=api+contract+backward+compatible+is:open&sort=created",
    ]

    seen_urls = set()
    for q in queries:
        full_query = f"{q}&per_page={min(limit, 20)}"
        data = _gh_search(full_query, endpoint="search/issues")
        if not data:
            continue

        for item in data.get("items", []):
            url = item.get("html_url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = item.get("title", "")
            body = (item.get("body") or "")[:500]
            reactions = item.get("reactions", {})
            total_reactions = sum(
                reactions.get(k, 0)
                for k in ["+1", "-1", "laugh", "hooray", "confused", "heart", "rocket", "eyes"]
            )

            # Calculate recency
            created_at = item.get("created_at", "")
            recency_days = 999.0
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    delta = datetime.now(timezone.utc) - created_dt
                    recency_days = delta.total_seconds() / 86400
                except (ValueError, TypeError):
                    pass

            pain = extract_pain_points(title, body)

            findings.append({
                "title": title,
                "url": url,
                "repo": "/".join(url.split("/")[3:5]) if "github.com" in url else "",
                "author": item.get("user", {}).get("login", ""),
                "reactions": total_reactions,
                "comments": item.get("comments", 0),
                "created_at": created_at,
                "recency_days": round(recency_days, 1),
                "category": "pain_thread",
                "pain_analysis": pain,
                "score": _score_finding(
                    "pain_thread",
                    recency_days=recency_days,
                    relevance_signals=len(pain.get("matched_categories", [])) + min(3, total_reactions),
                ),
            })

    findings.sort(key=lambda x: x["score"], reverse=True)
    return findings[:limit]


def hunter_scan(limit: int = 20) -> Dict[str, Any]:
    """Hunter scan: competitor users, adoption leads, pain threads."""
    competitor_users = _search_competitor_users(limit=limit)
    adoption_leads = _search_adoption_leads(limit=limit)
    pain_threads = _search_pain_threads(limit=limit)

    # Collect auto-ledger items
    auto_ledger_items = [
        f for f in competitor_users if f.get("auto_ledger")
    ]

    return {
        "cadence": "hunter",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "competitor_users": competitor_users,
        "adoption_leads": adoption_leads,
        "pain_threads": pain_threads,
        "summary": {
            "competitor_users_found": len(competitor_users),
            "adoption_leads_found": len(adoption_leads),
            "pain_threads_found": len(pain_threads),
            "auto_ledger_count": len(auto_ledger_items),
        },
        "auto_ledger_items": auto_ledger_items,
    }


# ---------------------------------------------------------------------------
#  Deep module (stub)
# ---------------------------------------------------------------------------


def deep_scan(limit: int = 20) -> Dict[str, Any]:
    """Deep scan: ecosystem intel and pain clustering (stub).

    Will include: competitor release tracking, MCP ecosystem mapping,
    weekly pain clustering, trending repo analysis.
    """
    return {
        "cadence": "deep",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "status": "stub",
        "message": "Deep scan not yet implemented. Use pulse or hunter.",
    }


# ---------------------------------------------------------------------------
#  Main orchestrator
# ---------------------------------------------------------------------------


def scan(cadence: str = "pulse", limit: int = 20) -> Dict[str, Any]:
    """Run a GitHub scan at the specified cadence.

    Args:
        cadence: pulse, hunter, or deep.
        limit: Max results per search query.

    Returns:
        Scan results dict with findings, scores, and metadata.
    """
    scan_start = datetime.now(timezone.utc)

    if cadence == "pulse":
        result = pulse_scan()
    elif cadence == "hunter":
        result = hunter_scan(limit=limit)
    elif cadence == "deep":
        result = deep_scan(limit=limit)
    else:
        return {"error": f"Unknown cadence: {cadence}. Supported: pulse, hunter, deep"}

    # Persist results
    _save_scan(result, cadence, scan_start)

    return result


def _save_scan(result: Dict[str, Any], cadence: str, scan_time: datetime) -> Path:
    """Save scan results to ~/.delimit/github_scans/{date}_{cadence}.json."""
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    filename = scan_time.strftime("%Y-%m-%dT%H%M%S") + f"_{cadence}.json"
    path = SCANS_DIR / filename
    path.write_text(json.dumps(result, indent=2, default=str))
    logger.info("GitHub scan saved to %s", path)
    return path
