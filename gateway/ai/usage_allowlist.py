"""Canonical usage allowlist for first-person experience claims in social drafts.

Single source of truth for the LED-1334 usage gate (2026-05-12 deliberation,
substantive unanimous). When a generated reply contains a first-person
experience clause (e.g. "saved me", "bit me on similar projects") paired with
a named third-party tool/service NOT on this allowlist, the validator blocks
the draft. The draft generation prompt is rendered from the same constant so
the prompt and validator cannot drift.

Why this exists: a Reddit draft on r/mcp/1t87arl fabricated "Pinning the
OpenAPI spec version you generated against and diffing on every Zoom release
saved me a bunch of mystery 400s." We don't use Zoom. The prior LED-1332
prompt rules and LED-1333 hardened claude CLI drafter both failed to catch it
because the OpenAPI-diff tactic IS real, but the claim of having USED IT ON
ZOOM was invented.

How to edit: change USAGE_ALLOWLIST below. The prompt's GROUND-TRUTH section
re-renders from this constant on every draft; the validator imports the same
constant; the parity test in tests/test_usage_gate.py asserts both consumers
see identical entries.
"""
from __future__ import annotations

import re
from typing import Iterable

# Canonical usage allowlist. Lowercase, normalized. One entry per tool/service
# that the founder ACTUALLY uses in their daily work and can speak to from
# lived experience. Adding an entry is a deliberate code change; PRs editing
# this file should describe the lived usage that backs the addition.
USAGE_ALLOWLIST: frozenset[str] = frozenset({
    # Coding agents (founder uses daily)
    "claude code", "claude-code", "claude",
    "codex", "codex cli", "codex-cli",
    "gemini", "gemini cli", "gemini-cli",
    "cursor",
    # Core protocol / standards we ship on
    "openapi", "openapi spec", "openapi schema",
    "mcp", "model context protocol", "mcp server",
    "github actions", "github action",
    # Attestation stack we ship
    "sigstore", "cosign", "rekor",
    # Languages / runtimes we ship in
    "python", "typescript", "javascript", "node", "npm",
    # Cloud / deploy we use
    "vercel",
    # API providers we call from the deliberation engine
    "anthropic api", "openai api", "vertex ai",
    # Our own product surface
    "delimit", "delimit-cli", "delimit-action", "delimit-mcp-server",
})


# First-person experience clauses that imply the speaker has lived/used the
# named subject. Detection here means "the draft is claiming standing to
# speak from use" — which must be backed by an allowlist match to be allowed.
_FIRST_PERSON_EXPERIENCE = re.compile(
    r"\b(?:"
    r"saved me|bit me|got me|caught me|surprised me|burned me|tripped me up|"
    r"what I[’']d do|the way I caught|in my experience|"
    r"when I (?:ran|used|tried|hit|wrapped|implemented|deployed|shipped|integrated)|"
    r"I (?:ran into|tripped over|got bit by|hit (?:this|that|the))|"
    r"I personally|from my own work|on similar projects|"
    r"mine still loads|mine kept (?:loading|breaking|drifting)|"
    r"I had to|I ended up"
    r")\b",
    re.IGNORECASE,
)


# Named-product extraction: proper-noun tokens, optionally compound, optionally
# with technical suffix (API/SDK/CLI/MCP). Tuned for permissive capture; the
# stopword set below filters obvious false positives.
_NAMED_PRODUCT = re.compile(
    r"\b([A-Z][a-zA-Z0-9.+-]*(?:\s+(?:[A-Z][a-zA-Z0-9.+-]*|API|SDK|CLI|MCP))*)\b"
)


# Tokens that pass the proper-noun regex but are not actually third-party
# product names. Keep this conservative — over-stopword'ing weakens the
# guardrail. Order: sentence-starters, generic verbs, common acronyms,
# concept words, our own ecosystem.
_NAMED_PRODUCT_STOPWORDS: frozenset[str] = frozenset({
    # Sentence-starters / pronouns
    "the", "a", "an", "i", "it", "they", "we", "you", "this", "that",
    # Question / conditional / temporal sentence-starters
    "when", "where", "why", "what", "who", "how", "which",
    "if", "but", "and", "or", "so", "then", "also",
    "since", "while", "before", "after", "until", "unless",
    "even", "though", "although",
    # Common verbs in capitalized sentence-start position
    "wrapping", "pinning", "running", "using", "trying", "diffing",
    "once", "first", "second", "third", "next", "then", "yes", "no",
    "yeah", "sure", "ok", "okay", "honestly", "curious", "neat", "cool",
    # Technical primitives (concepts, not products)
    "english", "ascii", "json", "yaml", "xml", "html", "css",
    "http", "https", "rest", "graphql", "websocket", "soap", "grpc",
    "tcp", "udp", "ssh", "tls", "ssl", "dns",
    "ci", "cd", "pr", "prs", "pull request", "merge", "commit",
    # OS / platforms
    "linux", "windows", "macos", "ios", "android", "unix",
    # Concept words people capitalize but aren't products
    "gateway", "proxy", "middleware", "scanner", "drafter",
    "markdown", "schema", "endpoint", "endpoints",
    "agent", "agents", "tool", "tools", "server", "servers", "client", "clients",
    # Generic words in sentence position
    "spend", "auto", "neat", "cool", "nice", "great", "interesting",
})


def _normalize(token: str) -> str:
    """Lowercase + collapse internal whitespace for allowlist comparison."""
    return " ".join(token.lower().split())


def is_on_allowlist(product: str) -> bool:
    """Return True if `product` matches an allowlist entry.

    Match is case-insensitive substring in both directions: a longer named
    product like "Claude Code SDK" matches the "claude code" allowlist entry,
    and a shorter named product like "MCP" matches "mcp server" by being
    contained in the allowlist entry.
    """
    normalized = _normalize(product)
    if not normalized:
        return False
    for entry in USAGE_ALLOWLIST:
        if entry in normalized or normalized in entry:
            return True
    return False


def extract_named_products(text: str) -> list[str]:
    """Extract candidate third-party product names from draft text.

    Returns deduped list of capitalized tokens that survived stopword
    filtering. The list is the candidate set the usage gate then checks
    against the allowlist.
    """
    if not text:
        return []
    raw = _NAMED_PRODUCT.findall(text)
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        normalized = _normalize(token)
        if not normalized or normalized in _NAMED_PRODUCT_STOPWORDS:
            continue
        # Drop ANY compound where ANY word is a stopword (sentence-starter
        # capitalization sweeping "When I ran" / "Once I hit" into a fake
        # compound product). Real product compounds like "Claude Code" /
        # "OpenAPI Spec" have no stopwords in them.
        parts = normalized.split(" ")
        if any(p in _NAMED_PRODUCT_STOPWORDS for p in parts):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(token)
    return out


def find_off_allowlist_experience_claims(text: str) -> list[dict]:
    """Detect first-person experience clauses paired with off-allowlist products.

    The LED-1334 usage gate. If the text contains a first-person experience
    clause AND mentions a named product not on the allowlist, returns a list
    of {clause, product} dicts. Empty list means the draft passes.

    Returns the FULL list (not first-match) so the orchestrator can surface
    all violations when blocking.
    """
    if not text:
        return []
    clause_match = _FIRST_PERSON_EXPERIENCE.search(text)
    if not clause_match:
        return []
    clause = clause_match.group(0)
    products = extract_named_products(text)
    violations: list[dict] = []
    for product in products:
        if is_on_allowlist(product):
            continue
        violations.append({"clause": clause, "product": product})
    return violations


def format_for_prompt() -> str:
    """Render the allowlist as a human-readable list for system-prompt injection.

    Output is deterministic (alphabetized) so prompt parity tests stay stable.
    """
    return "\n".join(f"  - {entry}" for entry in sorted(USAGE_ALLOWLIST))


def allowlist_as_sorted_tuple() -> tuple[str, ...]:
    """Return the allowlist as a sorted tuple for parity assertions in tests."""
    return tuple(sorted(USAGE_ALLOWLIST))
