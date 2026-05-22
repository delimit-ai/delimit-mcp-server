"""Ledger ship-state proof helpers (LED-1408 Phase 1).

When a ledger item transitions to `done`, we want auditable evidence that the
fix actually shipped. Two forms of proof:

1. **Commit-trailer binding** — the merge commit carries a `Ledger-Item:
   LED-NNNN` trailer. Lets a webhook or reconciler walk `git log
   origin/main` and find every commit-to-ledger link without naming
   conventions or fuzzy matching.

2. **PR-URL linkage** — `https://github.com/<org>/<repo>/pull/<N>` plus a
   verified `merged_at` timestamp. Fallback for items closed without a
   trailer.

Phase 1 (this module): parse + record. Items closed with proof get
`verified: true` on the event; items closed without proof get
`verified: false` so a future audit can find them.

Phase 2 (separate LED): the reconciler enforces stricter semantics —
items without proof default to `shipped_pending`, not `done`.

Memory anchor: feedback_agent_dashboard_done_means_committed_not_merged.md
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# Match `Ledger-Item: LED-1234` (case-insensitive, leading whitespace allowed).
# Pattern intentionally tolerant of trailing whitespace + multiple LED IDs:
# we extract the FIRST LED-N on the line.
_LEDGER_TRAILER_RE = re.compile(
    r"(?im)^\s*Ledger-Item\s*:\s*(LED-\d+)",
)

# Match a GitHub PR URL: https://github.com/<owner>/<repo>/pull/<N>
_PR_URL_RE = re.compile(
    r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
)


def parse_ledger_trailer(commit_message: str) -> Optional[str]:
    """Extract the `Ledger-Item: LED-NNNN` trailer value from a commit message.

    Returns the LED id (e.g. `LED-1408`) or None if no trailer is present.
    The trailer must be on its own line; mentions inside prose (e.g.
    `mentions LED-1408 in passing`) do NOT match.
    """
    if not commit_message:
        return None
    match = _LEDGER_TRAILER_RE.search(commit_message)
    if match:
        return match.group(1)
    return None


def parse_pr_url(text: str) -> Optional[Dict[str, str]]:
    """Extract the first GitHub PR URL from any string.

    Returns {owner, repo, number} or None.
    """
    if not text:
        return None
    match = _PR_URL_RE.search(text)
    if match:
        return {
            "owner": match.group(1),
            "repo": match.group(2),
            "number": match.group(3),
        }
    return None


def build_ship_proof(
    commit_sha: Optional[str] = None,
    pr_url: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, object]:
    """Build a ship-proof block to attach to a ledger `done` event.

    Inputs may come from explicit MCP-tool args OR from inline mentions
    in the note (the caller might paste a PR URL into the note field
    without realizing it's also queryable).

    Returns a dict with keys:
      - verified: bool — True iff commit_sha OR pr_url was provided
      - commit_sha: str or None
      - pr_url: str or None
      - pr_owner / pr_repo / pr_number: str or None (parsed from pr_url)
      - ledger_trailer: str or None (parsed from note, if present)

    The `verified` flag is the primary downstream signal. A future
    reconciler will refuse to transition `done` without verified=True;
    Phase 1 only records the flag without enforcing.
    """
    proof: Dict[str, object] = {
        "verified": bool(commit_sha or pr_url),
        "commit_sha": commit_sha or None,
        "pr_url": pr_url or None,
    }

    if pr_url:
        parsed = parse_pr_url(pr_url)
        if parsed:
            proof["pr_owner"] = parsed["owner"]
            proof["pr_repo"] = parsed["repo"]
            proof["pr_number"] = parsed["number"]

    # If pr_url not explicitly passed but appears in the note, capture it
    if not pr_url and note:
        parsed = parse_pr_url(note)
        if parsed:
            proof["pr_url"] = f"https://github.com/{parsed['owner']}/{parsed['repo']}/pull/{parsed['number']}"
            proof["pr_owner"] = parsed["owner"]
            proof["pr_repo"] = parsed["repo"]
            proof["pr_number"] = parsed["number"]
            proof["verified"] = True

    # Capture any Ledger-Item trailer from the note (rare but possible —
    # a worker might paste the commit message into the close note).
    if note:
        trailer = parse_ledger_trailer(note)
        if trailer:
            proof["ledger_trailer"] = trailer

    return proof
