"""
Delimit Dependency Manifest
Parses and validates .delimit/dependencies.yaml service dependency declarations.

Per Jamsons Doctrine:
- Deterministic outputs
- No credential discovery
- No telemetry
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("delimit.dependency_manifest")

# Manifest filename convention
MANIFEST_FILENAME = "dependencies.yaml"
MANIFEST_DIR = ".delimit"

# Valid characters for service identifiers: alphanumeric, hyphens, underscores, slashes
_SERVICE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")

# Required fields in a manifest
REQUIRED_FIELDS = frozenset(["service"])


def _load_yaml():
    """Lazy import yaml to avoid hard dependency."""
    try:
        import yaml
        return yaml
    except ImportError:
        return None


def normalize_service_id(service_id: str) -> str:
    """Normalize a service identifier to lowercase with consistent separators.

    Args:
        service_id: Raw service identifier string.

    Returns:
        Normalized lowercase identifier.
    """
    return service_id.strip().lower()


def validate_service_id(service_id: str) -> Optional[str]:
    """Validate a service identifier.

    Returns None if valid, or an error message string if invalid.
    """
    if not service_id or not isinstance(service_id, str):
        return "Service identifier must be a non-empty string"

    normalized = normalize_service_id(service_id)
    if not normalized:
        return "Service identifier is empty after normalization"

    if not _SERVICE_ID_PATTERN.match(normalized):
        return (
            f"Invalid service identifier: {service_id!r}. "
            "Must start with alphanumeric and contain only "
            "alphanumeric, hyphens, underscores, dots, or slashes."
        )

    return None


def validate_manifest(data: Dict[str, Any]) -> List[str]:
    """Validate a parsed dependency manifest.

    Returns a list of error strings. Empty list means valid.
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["Manifest must be a YAML mapping"]

    # Check required fields
    if "service" not in data:
        errors.append("Missing required field: 'service'")
    else:
        err = validate_service_id(data["service"])
        if err:
            errors.append(f"Invalid 'service' field: {err}")

    # Validate consumes list
    consumes = data.get("consumes")
    if consumes is not None:
        if not isinstance(consumes, list):
            errors.append("Field 'consumes' must be a list")
        else:
            for i, item in enumerate(consumes):
                if not isinstance(item, str):
                    errors.append(f"consumes[{i}] must be a string, got {type(item).__name__}")
                else:
                    err = validate_service_id(item)
                    if err:
                        errors.append(f"consumes[{i}]: {err}")

    # Validate produces list
    produces = data.get("produces")
    if produces is not None:
        if not isinstance(produces, list):
            errors.append("Field 'produces' must be a list")
        else:
            for i, item in enumerate(produces):
                if not isinstance(item, str):
                    errors.append(f"produces[{i}] must be a string, got {type(item).__name__}")
                else:
                    err = validate_service_id(item)
                    if err:
                        errors.append(f"produces[{i}]: {err}")

    # Validate optional string fields
    for field in ("owner", "repository"):
        val = data.get(field)
        if val is not None and not isinstance(val, str):
            errors.append(f"Field '{field}' must be a string")

    return errors


def parse_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse and normalize a dependency manifest.

    Args:
        data: Raw parsed YAML dictionary.

    Returns:
        Normalized manifest dictionary with deterministic structure.

    Raises:
        ValueError: If validation fails.
    """
    errors = validate_manifest(data)
    if errors:
        raise ValueError(f"Manifest validation failed: {'; '.join(errors)}")

    manifest = {
        "service": normalize_service_id(data["service"]),
        "consumes": sorted(set(
            normalize_service_id(s) for s in data.get("consumes", [])
        )),
        "produces": sorted(set(
            normalize_service_id(s) for s in data.get("produces", [])
        )),
    }

    if "owner" in data and isinstance(data["owner"], str):
        manifest["owner"] = data["owner"].strip()
    if "repository" in data and isinstance(data["repository"], str):
        manifest["repository"] = data["repository"].strip()

    return manifest


def load_manifest_file(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """Load and parse a dependency manifest from a YAML file.

    Args:
        path: Path to the dependencies.yaml file.

    Returns:
        Parsed manifest dictionary, or None if the file doesn't exist
        or can't be parsed.
    """
    yaml = _load_yaml()
    if yaml is None:
        logger.warning("PyYAML not installed — cannot parse dependency manifests")
        return None

    filepath = Path(path)
    if not filepath.exists():
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning("Failed to load manifest %s: %s", filepath, e)
        return None

    if data is None:
        return None

    try:
        return parse_manifest(data)
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("Invalid manifest %s: %s", filepath, e)
        return None


def discover_manifests(root_dir: Union[str, Path]) -> List[Dict[str, Any]]:
    """Discover and load all dependency manifests under a root directory.

    Searches for .delimit/dependencies.yaml files recursively.

    Args:
        root_dir: Root directory to search.

    Returns:
        List of parsed manifest dictionaries, sorted by service name.
    """
    root = Path(root_dir)
    manifests = []

    if not root.exists():
        return manifests

    # Search for manifest files
    for manifest_path in root.rglob(f"{MANIFEST_DIR}/{MANIFEST_FILENAME}"):
        manifest = load_manifest_file(manifest_path)
        if manifest is not None:
            manifest["_source_path"] = str(manifest_path)
            manifests.append(manifest)

    # Deterministic ordering
    manifests.sort(key=lambda m: m["service"])
    return manifests
