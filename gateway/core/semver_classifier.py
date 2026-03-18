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

BREAKING_TYPES = frozenset({
    ChangeType.ENDPOINT_REMOVED,
    ChangeType.METHOD_REMOVED,
    ChangeType.REQUIRED_PARAM_ADDED,
    ChangeType.PARAM_REMOVED,
    ChangeType.RESPONSE_REMOVED,
    ChangeType.REQUIRED_FIELD_ADDED,
    ChangeType.FIELD_REMOVED,
    ChangeType.TYPE_CHANGED,
    ChangeType.FORMAT_CHANGED,
    ChangeType.ENUM_VALUE_REMOVED,
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
        if change.type in BREAKING_TYPES:
            has_breaking = True
            break  # short-circuit — can't go higher than MAJOR
        if change.type in ADDITIVE_TYPES:
            has_additive = True

    if has_breaking:
        return SemverBump.MAJOR
    if has_additive:
        return SemverBump.MINOR
    return SemverBump.PATCH


def classify_detailed(changes: List[Change]) -> Dict[str, Any]:
    """Return a detailed classification with per-category breakdowns.

    Used by CLI explain and PR comment generation.
    """
    bump = classify(changes)

    breaking = [c for c in changes if c.type in BREAKING_TYPES]
    additive = [c for c in changes if c.type in ADDITIVE_TYPES]
    patch = [c for c in changes if c.type in PATCH_TYPES]

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
