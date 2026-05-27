"""
Delimit Semver Classifier

Deterministic semver bump classification from diff engine output.
Takes a list of Change objects and returns the recommended version bump.

Rules:
  - Any breaking change → MAJOR
  - Any additive change (no breaking) → MINOR
  - Non-functional changes only → PATCH
  - No changes → NONE
"""

from enum import Enum
from typing import Any, Dict, List

from .diff_engine_v2 import Change, ChangeType


class SemverBump(Enum):
    NONE = "none"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


# ── Change-type buckets ──────────────────────────────────────────────

# LED-1600: this bucket MUST stay aligned with diff_engine_v2.Change.is_breaking.
# Previously it listed only 10 of the engine's breaking types, so a
# PARAM_REQUIRED_CHANGED, RESPONSE_TYPE_CHANGED, SECURITY_REMOVED,
# SECURITY_SCOPE_REMOVED, MAX_LENGTH_DECREASED, MIN_LENGTH_INCREASED or
# PARAM_TYPE_CHANGED — all of which the engine flags `is_breaking=True` — was
# silently classified MINOR instead of MAJOR. That is the exact "silent semver
# minor->major leak" this LED closes: a breaking change slipping through as
# non-breaking is the worst failure mode for a merge gate. The context-
# sensitive field types (FIELD_REMOVED / REQUIRED_FIELD_ADDED /
# FIELD_REQUIREMENT_RELAXED) are NOT listed here as unconditional — their
# breaking-ness depends on request/response direction and is read per-Change
# via `is_breaking` in classify() below.
BREAKING_TYPES = frozenset({
    ChangeType.ENDPOINT_REMOVED,
    ChangeType.METHOD_REMOVED,
    ChangeType.REQUIRED_PARAM_ADDED,
    ChangeType.PARAM_REMOVED,
    ChangeType.RESPONSE_REMOVED,
    ChangeType.TYPE_CHANGED,
    ChangeType.FORMAT_CHANGED,
    ChangeType.ENUM_VALUE_REMOVED,
    ChangeType.PARAM_TYPE_CHANGED,
    ChangeType.PARAM_REQUIRED_CHANGED,
    ChangeType.RESPONSE_TYPE_CHANGED,
    ChangeType.SECURITY_REMOVED,
    ChangeType.SECURITY_SCOPE_REMOVED,
    ChangeType.MAX_LENGTH_DECREASED,
    ChangeType.MIN_LENGTH_INCREASED,
})

# Context-sensitive types whose breaking-ness is decided per-Change by the
# engine's direction-aware is_breaking, not by membership in BREAKING_TYPES.
CONTEXT_SENSITIVE_TYPES = frozenset({
    ChangeType.FIELD_REMOVED,
    ChangeType.REQUIRED_FIELD_ADDED,
    ChangeType.FIELD_REQUIREMENT_RELAXED,
})

ADDITIVE_TYPES = frozenset({
    ChangeType.ENDPOINT_ADDED,
    ChangeType.METHOD_ADDED,
    ChangeType.OPTIONAL_PARAM_ADDED,
    ChangeType.RESPONSE_ADDED,
    ChangeType.OPTIONAL_FIELD_ADDED,
    ChangeType.ENUM_VALUE_ADDED,
})

PATCH_TYPES = frozenset({
    ChangeType.DESCRIPTION_CHANGED,
})


def classify(changes: List[Change]) -> SemverBump:
    """Classify a list of changes into a semver bump level.

    Deterministic: same input always produces same output.
    """
    if not changes:
        return SemverBump.NONE

    has_breaking = False
    has_additive = False

    for change in changes:
        # LED-1600: the engine's is_breaking is the single source of truth.
        # For context-sensitive types it already encodes request/response
        # direction; for everything else it matches BREAKING_TYPES membership.
        # Using it here closes the gap where a breaking change (e.g. an
        # optional->required param, or a response field removal) was bucketed
        # MINOR. `getattr` keeps the function working for duck-typed Change-
        # likes that may not carry the property.
        if _is_breaking(change):
            has_breaking = True
            break  # short-circuit — can't go higher than MAJOR
        if change.type in ADDITIVE_TYPES:
            has_additive = True

    if has_breaking:
        return SemverBump.MAJOR
    if has_additive:
        return SemverBump.MINOR
    return SemverBump.PATCH


def _is_breaking(change) -> bool:
    """Authoritative breaking check for a Change.

    Prefers the engine's direction-aware ``is_breaking`` property. Falls back
    to BREAKING_TYPES membership for objects that don't expose it (e.g. test
    doubles). Context-sensitive types are only breaking when ``is_breaking``
    says so; never inferred from the type alone.
    """
    val = getattr(change, "is_breaking", None)
    if isinstance(val, bool):
        return val
    return getattr(change, "type", None) in BREAKING_TYPES


def classify_detailed(changes: List[Change]) -> Dict[str, Any]:
    """Return a detailed classification with per-category breakdowns.

    Used by CLI explain and PR comment generation.
    """
    bump = classify(changes)

    breaking = [c for c in changes if _is_breaking(c)]
    additive = [c for c in changes if not _is_breaking(c) and c.type in ADDITIVE_TYPES]
    patch = [c for c in changes if not _is_breaking(c) and c.type in PATCH_TYPES]

    return {
        "bump": bump.value,
        "is_breaking": bump == SemverBump.MAJOR,
        "counts": {
            "total": len(changes),
            "breaking": len(breaking),
            "additive": len(additive),
            "patch": len(patch),
        },
        "breaking_changes": [
            {"type": c.type.value, "path": c.path, "message": c.message}
            for c in breaking
        ],
        "additive_changes": [
            {"type": c.type.value, "path": c.path, "message": c.message}
            for c in additive
        ],
        "patch_changes": [
            {"type": c.type.value, "path": c.path, "message": c.message}
            for c in patch
        ],
    }


def bump_version(current: str, bump: SemverBump) -> str:
    """Apply a semver bump to a version string.

    Args:
        current: Version string like "1.2.3" or "v1.2.3".
        bump: The bump level to apply.

    Returns:
        New version string (preserves 'v' prefix if present).
    """
    prefix = ""
    ver = current
    if ver.startswith("v"):
        prefix = "v"
        ver = ver[1:]

    parts = ver.split(".")
    if len(parts) != 3:
        return current  # can't parse — return unchanged

    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return current

    if bump == SemverBump.MAJOR:
        major += 1
        minor = 0
        patch = 0
    elif bump == SemverBump.MINOR:
        minor += 1
        patch = 0
    elif bump == SemverBump.PATCH:
        patch += 1
    # NONE: no change

    return f"{prefix}{major}.{minor}.{patch}"
