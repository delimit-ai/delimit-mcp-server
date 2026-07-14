"""Strategic-bar wedge classifier for scan-bridge promotions.

The github-scan → ledger pipeline flooded the strategy ledger with 1,467
auto-generated "STRATEGIC:" items at a ~1.6% action rate. The LED-1724
precision layer (``bridge._passes_precision``) is a *negative* filter — it
rejects only the clearly-noise (generic-agent-tool-only / codegen
false-friends) and PASSES anything uncertain. That is recall-favoring by
design, but it still lets a large "uncertain" middle into the ledger.

This module adds the *positive* strategic bar the founder asked for: a
finding may auto-create a STRATEGY ledger item ONLY when it lands inside
Delimit's WEDGE. Everything else (including the uncertain middle) is routed
to the digest / intel-snapshot sink instead of the ledger.

The wedge (two arms):

  1. **API arm** — OpenAPI / API breaking-change detection & contract CI.
     The signal text carries a concrete API-governance keyword (openapi,
     breaking change, semver, spec diff, oasdiff, spectral, api contract,
     contract test, deprecation, api versioning, …) AND is not a codegen
     false-friend (spec→server/CLI generators are NOT diff/governance
     competitors).

  2. **Orchestration arm** — governance / policy / orchestration layers for
     AI coding assistants (Claude Code / Codex / Gemini / Cursor). The signal
     text carries a governance/policy/orchestration token (governance,
     policy, guardrail, merge gate, review gate, ci gate, orchestration,
     control plane, …) AND an AI-coding-assistant token (claude code, codex,
     gemini, cursor, coding assistant, ai agent, copilot, …). A governance
     token alone (generic CI policy) or an assistant token alone (a random
     MCP server / dotfiles repo) is NOT enough.

Design rules (mirror the LED-1724 precision layer):
  * Small, pure, unit-testable ``classify_wedge(signal) -> (in_wedge, arm,
    reason)``. No I/O, no network.
  * Every keyword set is env-overridable (``DELIMIT_SCAN_WEDGE_*``) so the
    founder can retune without a code change.
  * A reversible kill-switch (``DELIMIT_SCAN_WEDGE=off``) restores the exact
    pre-wedge behavior (precision layer only). Default ON — noise reduction
    is the whole point (founder rule: flags default enabled or not at all).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

# ── API arm — concrete API-governance / breaking-change keywords ──────
# A signal carrying ANY of these (and NOT a codegen false-friend) is in the
# wedge's API arm. Kept aligned with bridge._DOMAIN_KEYWORDS_DEFAULT.
_API_KEYWORDS_DEFAULT = (
    "openapi",
    "breaking change",
    "breaking changes",
    "semver",
    "semantic version",
    "migration guide",
    "api contract",
    "contract test",
    "contract testing",
    "spec diff",
    "spec-diff",
    "schema diff",
    "schema-diff",
    "diff engine",
    "api governance",
    "api diff",
    "attestation",
    "merge gate",
    "merge-gate",
    "spectral",
    "oasdiff",
    "openapi-changes",
    "api versioning",
    "deprecation",
    "graphql schema",
    "json schema",
    "swagger",
)

# Codegen false-friends: "openapi" appears but it's a spec→server/CLI
# generator, NOT a spec-diff / governance competitor. Mirrors
# bridge._FALSE_FRIEND_PATTERNS_DEFAULT.
_FALSE_FRIEND_PATTERNS_DEFAULT = (
    "openapi spec into",
    "openapi into",
    "openapi to mcp",
    "openapi -> mcp",
    "openapi->mcp",
    "openapi to cli",
    "openapi -> cli",
    "spec into an mcp server",
    "mcp server from openapi",
    "generate a cli from",
    "turn any openapi",
    "convert openapi",
)

# ── Orchestration arm — governance/policy/orchestration tokens ────────
_GOVERNANCE_TOKENS_DEFAULT = (
    "governance",
    "policy engine",
    "policy kernel",
    "guardrail",
    "guardrails",
    "orchestration",
    "orchestrator",
    "merge gate",
    "review gate",
    "ci gate",
    "quality gate",
    "approval gate",
    "compliance gate",
    "gatekeeper",
    "control plane",
    "policy as code",
    "fail-closed",
    "fail closed",
    "audit trail",
    "attestation",
)

# ── Orchestration arm — AI-coding-assistant tokens ────────────────────
_ASSISTANT_TOKENS_DEFAULT = (
    "claude code",
    "claude-code",
    "codex",
    "gemini cli",
    "gemini-cli",
    "cursor",
    "copilot",
    "coding assistant",
    "coding agent",
    "ai coding",
    "ai-written code",
    "ai written code",
    "ai agent",
    "ai agents",
    "llm agent",
    "agentic",
)


def _env_keyword_set(env_name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    """Resolve an env-overridable comma-separated keyword set.

    Empty / unset → the hardcoded default; otherwise split on commas,
    lowercase, strip, drop empties. On any failure fall back to default.
    """
    raw = os.environ.get(env_name, "")
    if not raw:
        return default
    try:
        parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
        return parts or default
    except Exception:  # pragma: no cover — defensive
        return default


def wedge_enabled() -> bool:
    """Reversible kill-switch. ``DELIMIT_SCAN_WEDGE=off`` (or
    false/0/no/disable) disables the strategic-bar wedge filter, restoring
    pre-wedge behavior (precision layer only). Default ON.
    """
    raw = os.environ.get("DELIMIT_SCAN_WEDGE", "").strip().lower()
    if raw in {"off", "0", "false", "no", "disable", "disabled"}:
        return False
    return True


def _signal_text(signal: Dict[str, Any]) -> str:
    snippet = (signal.get("content_snippet") or "").lower()
    rationale = (signal.get("rationale") or "").lower()
    title = (signal.get("title") or "").lower()
    return f"{snippet}\n{rationale}\n{title}"


def classify_wedge(signal: Dict[str, Any]) -> Tuple[bool, str, str]:
    """Classify a scanned signal against Delimit's wedge.

    Returns ``(in_wedge, arm, reason)`` where ``arm`` is ``"api"``,
    ``"orchestration"``, or ``""`` (not in wedge). Pure — no I/O.
    """
    text = _signal_text(signal)

    api_kws = _env_keyword_set("DELIMIT_SCAN_WEDGE_API_KEYWORDS", _API_KEYWORDS_DEFAULT)
    false_friends = _env_keyword_set(
        "DELIMIT_SCAN_WEDGE_FALSE_FRIENDS", _FALSE_FRIEND_PATTERNS_DEFAULT
    )
    gov_tokens = _env_keyword_set(
        "DELIMIT_SCAN_WEDGE_GOVERNANCE_TOKENS", _GOVERNANCE_TOKENS_DEFAULT
    )
    assistant_tokens = _env_keyword_set(
        "DELIMIT_SCAN_WEDGE_ASSISTANT_TOKENS", _ASSISTANT_TOKENS_DEFAULT
    )

    is_false_friend = any(ff in text for ff in false_friends)

    # API arm: a concrete API-governance keyword, not a codegen false-friend.
    api_hit = next((kw for kw in api_kws if kw in text), "")
    if api_hit and not is_false_friend:
        return True, "api", f"wedge:api ({api_hit})"

    # Orchestration arm: governance/policy/orchestration token AND an
    # AI-coding-assistant token together.
    gov_hit = next((kw for kw in gov_tokens if kw in text), "")
    asst_hit = next((kw for kw in assistant_tokens if kw in text), "")
    if gov_hit and asst_hit:
        return True, "orchestration", f"wedge:orchestration ({gov_hit} + {asst_hit})"

    # Not in wedge — route to digest / intel snapshot, not the ledger.
    if is_false_friend:
        return False, "", "non-wedge: openapi codegen false-friend"
    if asst_hit and not gov_hit:
        return False, "", f"non-wedge: assistant token ({asst_hit}) without governance layer"
    if gov_hit and not asst_hit:
        return False, "", f"non-wedge: governance token ({gov_hit}) without AI-assistant context"
    return False, "", "non-wedge: no API or orchestration wedge match"
