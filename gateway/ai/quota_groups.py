"""Quota group mapping and hold-key helpers for the Antigravity CLI (agy).

The Antigravity CLI has TWO separate quota groups per account:
  - GEMINI ('gemini'): Gemini Flash, Gemini Pro
  - CLAUDE_GPT ('claude_gpt'): Claude Opus, Claude Sonnet, GPT models

Each group has its own weekly and 5-hour limits. This module provides a single
shared authority for classifying models/runtimes into quota groups and building
uniform hold and route keys across deliberation, worker dispatch, provider usage,
and swarm admission.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


GROUP_GEMINI: str = "gemini"
GROUP_CLAUDE_GPT: str = "claude_gpt"
ALL_GROUPS: Tuple[str, str] = (GROUP_GEMINI, GROUP_CLAUDE_GPT)

# Indicators in model ids, candidate lists, display names, or error evidence
_CLAUDE_GPT_KEYWORDS: Tuple[str, ...] = ("claude", "gpt", "opus", "sonnet", "haiku")
_GEMINI_KEYWORDS: Tuple[str, ...] = ("gemini",)

_CLAUDE_GPT_EVIDENCE_RE = re.compile(
    r"\b(?:claude|gpt|opus|sonnet|haiku|claude[-_]gpt)\b", re.IGNORECASE
)
_GEMINI_EVIDENCE_RE = re.compile(
    r"\b(?:gemini)\b", re.IGNORECASE
)


def normalize_quota_group(group: Optional[str]) -> Optional[str]:
    """Normalize a quota group string to 'gemini' or 'claude_gpt', else None."""
    if not group:
        return None
    raw = str(group).strip().lower()
    if raw in (GROUP_GEMINI,):
        return GROUP_GEMINI
    if raw in (GROUP_CLAUDE_GPT, "claude-gpt", "claude", "gpt"):
        return GROUP_CLAUDE_GPT
    return None


def is_antigravity_runtime(runtime: Optional[str]) -> bool:
    """Return True if runtime matches any Antigravity CLI runtime identifier.

    Recognises every form quota_groups.py supports:
        - 'antigravity', 'agy'
        - 'antigravity:*', 'agy:*'
        - 'antigravity_*', 'agy_*'
    """
    if not runtime:
        return False
    rt = str(runtime).strip().lower()
    return rt in ("antigravity", "agy") or rt.startswith(
        ("antigravity:", "antigravity_", "agy:", "agy_")
    )


def antigravity_quota_group(
    model_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    evidence: str = "",
) -> Optional[str]:
    """Derive the Antigravity quota group ('gemini' vs 'claude_gpt').

    Returns:
        'gemini' for Gemini-family models or default Antigravity worker usage.
        'claude_gpt' for Claude/GPT models riding Antigravity.
        None for unknown models (fails closed to shared unpartitioned handling).
    """
    cfg = config if isinstance(config, dict) else {}

    # 0. Explicit config override wins when valid.
    explicit = str(cfg.get("quota_group", "") or "").strip().lower()
    if explicit:
        norm = normalize_quota_group(explicit)
        if norm:
            return norm
        if explicit in ("gemini", "claude_gpt", "claude-gpt"):
            return "claude_gpt" if "claude" in explicit or "gpt" in explicit else "gemini"

    mid = str(model_id or "").strip().lower()
    model_str = str(cfg.get("model", "") or "").strip().lower()
    name = str(cfg.get("name", "") or "").strip().lower()
    raw_cands = cfg.get("model_candidates")
    cands: List[str] = []
    if isinstance(raw_cands, (list, tuple)):
        cands = [str(c).strip().lower() for c in raw_cands if c]

    # 1. Claude/GPT models: check model string, candidates, id, name.
    # Must precede Gemini detection because aliases like `claude_antigravity`
    # contain both 'claude' and 'antigravity'.
    for s in [model_str, *cands, mid, name]:
        if not s:
            continue
        if any(k in s for k in _CLAUDE_GPT_KEYWORDS):
            return GROUP_CLAUDE_GPT

    # Vendor check for Anthropic / OpenAI.
    for s in (mid, model_str, name):
        if not s:
            continue
        if s.startswith("claude") or s.startswith("fable") or "claude" in s:
            return GROUP_CLAUDE_GPT
        if s.startswith("codex") or s.startswith("gpt") or "codex" in s:
            return GROUP_CLAUDE_GPT

    # 2. Gemini models: inspect model string, candidates, id, name.
    for s in [model_str, *cands, mid, name]:
        if not s:
            continue
        if any(k in s for k in _GEMINI_KEYWORDS):
            return GROUP_GEMINI

    # Vendor check for Google.
    for s in (mid, model_str, name):
        if not s:
            continue
        if s.startswith("gemini") or s.startswith("vertex") or "gemini" in s:
            return GROUP_GEMINI

    # 3. If evidence was provided and specifies a group, inspect it.
    if evidence:
        if _CLAUDE_GPT_EVIDENCE_RE.search(evidence):
            return GROUP_CLAUDE_GPT
        if _GEMINI_EVIDENCE_RE.search(evidence):
            return GROUP_GEMINI

    # 4. Seat/runtime base names for Antigravity (default model is Gemini).
    # If a specific unknown model was passed, do not collapse it into Gemini!
    has_specific_unrecognized_model = bool(model_str and not any(k in model_str for k in ("antigravity", "agy", "gemini")))
    if has_specific_unrecognized_model and mid not in ("antigravity", "agy", "gemini"):
        return None

    if mid in ("antigravity", "agy", "antigravity_gemini", "agy_gemini"):
        return GROUP_GEMINI
    if name in ("antigravity", "agy") or name.startswith("antigravity") or name.startswith("agy"):
        return GROUP_GEMINI

    return None


def deliberation_route_key(group: Optional[str]) -> str:
    """Build the deliberation account route key for a quota group.

    'gemini'     -> 'cli:google-cli:gemini'
    'claude_gpt' -> 'cli:google-cli:claude_gpt'
    None/other   -> 'cli:google-cli' (fail closed)
    """
    norm = normalize_quota_group(group)
    if norm in (GROUP_GEMINI, GROUP_CLAUDE_GPT):
        return f"cli:google-cli:{norm}"
    return "cli:google-cli"


def deliberation_cache_key(route_key: str) -> str:
    """Deliberation route availability cache key."""
    return f"route:{route_key}"


def deliberation_legacy_cache_key(route_key: str) -> Optional[str]:
    """Return the legacy cache key for backward compatibility, if applicable."""
    if route_key.startswith("cli:google-cli:"):
        return "route:cli:google-cli"
    return None


def dispatch_hold_key(runtime: str, group: Optional[str] = None) -> str:
    """Primary key for recording a dispatch runtime hold."""
    rt = str(runtime or "").strip().lower()
    if is_antigravity_runtime(rt):
        base = "antigravity"
        norm = normalize_quota_group(group)
        if norm:
            return f"{base}:{norm}"
        if ":" in rt:
            suffix = rt.split(":", 1)[1]
            norm_s = normalize_quota_group(suffix)
            if norm_s:
                return f"{base}:{norm_s}"
        if "_" in rt:
            suffix = rt.split("_", 1)[1]
            norm_s = normalize_quota_group(suffix)
            if norm_s:
                return f"{base}:{norm_s}"
        return base
    return rt


def dispatch_hold_candidate_keys(runtime: str, group: Optional[str] = None) -> List[str]:
    """Candidate keys to check in holds storage for a runtime and group.

    Always includes legacy unpartitioned keys for backward compatibility.
    """
    rt = str(runtime or "").strip().lower()
    if is_antigravity_runtime(rt):
        norm = normalize_quota_group(group)
        if not norm and ":" in rt:
            norm = normalize_quota_group(rt.split(":", 1)[1])
        if not norm and "_" in rt:
            norm = normalize_quota_group(rt.split("_", 1)[1])
        if norm:
            keys = [
                f"antigravity:{norm}",
                f"antigravity_{norm}",
                f"agy:{norm}",
                f"agy_{norm}",
            ]
            if norm == GROUP_CLAUDE_GPT:
                keys.extend([
                    "antigravity:claude-gpt",
                    "antigravity_claude-gpt",
                    "agy:claude-gpt",
                    "agy_claude-gpt",
                    "claude_antigravity",
                ])
            elif norm == GROUP_GEMINI:
                keys.extend(["gemini", "gemini_consumer"])
            # Legacy unpartitioned keys
            keys.extend(["antigravity", "agy"])
            return keys
        else:
            # Unknown group: must check any hold on antigravity (fail closed)
            return [
                "antigravity",
                "agy",
                f"antigravity:{GROUP_GEMINI}",
                f"antigravity_{GROUP_GEMINI}",
                f"agy:{GROUP_GEMINI}",
                f"agy_{GROUP_GEMINI}",
                f"antigravity:{GROUP_CLAUDE_GPT}",
                f"antigravity_{GROUP_CLAUDE_GPT}",
                f"agy:{GROUP_CLAUDE_GPT}",
                f"agy_{GROUP_CLAUDE_GPT}",
                "antigravity:claude-gpt",
                "antigravity_claude-gpt",
                "agy:claude-gpt",
                "agy_claude-gpt",
                "claude_antigravity",
                "gemini",
                "gemini_consumer",
            ]
    return [rt]
