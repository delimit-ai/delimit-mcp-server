"""
Delimit Event Schema
Canonical event schema for API contract evolution tracking.
Deterministic validation and serialization per Jamsons Doctrine.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# Schema version for forward compatibility
SCHEMA_VERSION = "1.0.0"

# Valid event types
VALID_EVENT_TYPES = frozenset([
    "contract_change",
    "contract_added",
    "contract_removed",
    "policy_evaluation",
    "complexity_assessment",
    "baseline_established",
])

# Required top-level fields
REQUIRED_FIELDS = frozenset([
    "event_type",
    "api_name",
    "repository",
    "version",
    "timestamp",
    "commit",
    "actor",
    "spec_hash",
    "previous_hash",
    "diff_summary",
    "policy_result",
    "complexity_score",
    "complexity_class",
    "event_hash",
])

# Valid complexity classes
VALID_COMPLEXITY_CLASSES = frozenset([
    "simple",
    "moderate",
    "complex",
    "enterprise",
])

# Valid policy results
VALID_POLICY_RESULTS = frozenset([
    "passed",
    "failed",
    "warning",
    "skipped",
])

# SHA-256 hex pattern
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

# ISO 8601 UTC pattern
_ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
)


def validate_event(event: Dict[str, Any]) -> List[str]:
    """Validate an event payload against the canonical schema.

    Returns a list of validation error strings. Empty list means valid.
    """
    errors: List[str] = []

    # Check required fields
    missing = REQUIRED_FIELDS - set(event.keys())
    if missing:
        errors.append(f"Missing required fields: {sorted(missing)}")

    # Validate event_type
    event_type = event.get("event_type")
    if event_type is not None and event_type not in VALID_EVENT_TYPES:
        errors.append(
            f"Invalid event_type: {event_type!r}. "
            f"Must be one of: {sorted(VALID_EVENT_TYPES)}"
        )

    # Validate string fields are non-empty strings
    string_fields = [
        "api_name", "repository", "version", "commit", "actor",
        "spec_hash", "previous_hash", "event_hash",
    ]
    for field in string_fields:
        val = event.get(field)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            errors.append(f"Field {field!r} must be a non-empty string")

    # Validate timestamp format (ISO 8601 UTC)
    ts = event.get("timestamp")
    if ts is not None:
        if not isinstance(ts, str) or not _ISO8601_PATTERN.match(ts):
            errors.append(
                f"Field 'timestamp' must be ISO 8601 UTC format "
                f"(YYYY-MM-DDTHH:MM:SSZ), got: {ts!r}"
            )

    # Validate spec_hash format
    spec_hash = event.get("spec_hash")
    if spec_hash is not None and isinstance(spec_hash, str):
        if spec_hash != "GENESIS" and not _SHA256_PATTERN.match(spec_hash):
            errors.append(
                f"Field 'spec_hash' must be a SHA-256 hex string, "
                f"got: {spec_hash!r}"
            )

    # Validate previous_hash format
    prev_hash = event.get("previous_hash")
    if prev_hash is not None and isinstance(prev_hash, str):
        if prev_hash != "GENESIS" and not _SHA256_PATTERN.match(prev_hash):
            errors.append(
                f"Field 'previous_hash' must be 'GENESIS' or SHA-256 hex, "
                f"got: {prev_hash!r}"
            )

    # Validate event_hash format
    event_hash = event.get("event_hash")
    if event_hash is not None and isinstance(event_hash, str):
        if not _SHA256_PATTERN.match(event_hash):
            errors.append(
                f"Field 'event_hash' must be a SHA-256 hex string, "
                f"got: {event_hash!r}"
            )

    # Validate diff_summary is a list
    diff_summary = event.get("diff_summary")
    if diff_summary is not None and not isinstance(diff_summary, list):
        errors.append("Field 'diff_summary' must be a list")

    # Validate policy_result
    policy_result = event.get("policy_result")
    if policy_result is not None and policy_result not in VALID_POLICY_RESULTS:
        errors.append(
            f"Invalid policy_result: {policy_result!r}. "
            f"Must be one of: {sorted(VALID_POLICY_RESULTS)}"
        )

    # Validate complexity_score is an integer 0-100
    score = event.get("complexity_score")
    if score is not None:
        if not isinstance(score, int) or score < 0 or score > 100:
            errors.append(
                f"Field 'complexity_score' must be an integer 0-100, "
                f"got: {score!r}"
            )

    # Validate complexity_class
    cclass = event.get("complexity_class")
    if cclass is not None and cclass not in VALID_COMPLEXITY_CLASSES:
        errors.append(
            f"Invalid complexity_class: {cclass!r}. "
            f"Must be one of: {sorted(VALID_COMPLEXITY_CLASSES)}"
        )

    return errors


def canonicalize(event: Dict[str, Any]) -> str:
    """Serialize event to canonical JSON with deterministic key ordering.

    Uses sorted keys and no unnecessary whitespace for reproducibility.
    This ensures identical events always produce identical byte sequences.
    """
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def compute_event_hash(
    previous_hash: str,
    spec_hash: str,
    diff_summary: List[Any],
    commit: str,
    timestamp: str,
) -> str:
    """Compute deterministic SHA-256 event hash.

    Hash inputs are concatenated in a fixed, documented order:
        previous_hash + spec_hash + canonical(diff_summary) + commit + timestamp

    Returns lowercase hex digest.
    """
    diff_canonical = json.dumps(diff_summary, sort_keys=True, separators=(",", ":"))
    payload = (
        previous_hash
        + spec_hash
        + diff_canonical
        + commit
        + timestamp
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_event(
    event_type: str,
    api_name: str,
    repository: str,
    version: str,
    timestamp: str,
    commit: str,
    actor: str,
    spec_hash: str,
    previous_hash: str,
    diff_summary: List[Any],
    policy_result: str,
    complexity_score: int,
    complexity_class: str,
    schema_version: str = SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Create a validated event with computed hash.

    Raises ValueError if the resulting event fails validation.
    """
    event_hash = compute_event_hash(
        previous_hash=previous_hash,
        spec_hash=spec_hash,
        diff_summary=diff_summary,
        commit=commit,
        timestamp=timestamp,
    )

    event = {
        "schema_version": schema_version,
        "event_type": event_type,
        "api_name": api_name,
        "repository": repository,
        "version": version,
        "timestamp": timestamp,
        "commit": commit,
        "actor": actor,
        "spec_hash": spec_hash,
        "previous_hash": previous_hash,
        "diff_summary": diff_summary,
        "policy_result": policy_result,
        "complexity_score": complexity_score,
        "complexity_class": complexity_class,
        "event_hash": event_hash,
    }

    errors = validate_event(event)
    if errors:
        raise ValueError(f"Event validation failed: {'; '.join(errors)}")

    return event


def now_utc() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
