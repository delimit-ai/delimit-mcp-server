"""
OpenAPI version detection and compatibility metadata.

Centralizes the list of OpenAPI specification versions Delimit's diff/lint
engines accept. The diff engine itself is structurally version-agnostic --
it walks the spec dict regardless of declared version -- but having an
explicit allowlist lets us:

  1. Warn users early when a spec uses an unknown version (typo, future
     version we have not validated yet, etc).
  2. Surface the detected version in tool responses so CI systems can log it.
  3. Centralize "what does Delimit support" so README / docs / detector all
     read from the same source of truth.

LED-290: bumped to include OpenAPI 3.2.0 (released 2025).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("delimit.core.openapi_version")

# Major.minor families Delimit's diff/lint engines have been validated against.
# Patch versions inside each family (e.g., 3.0.0, 3.0.1, 3.0.3) are all accepted.
SUPPORTED_OPENAPI_FAMILIES: Tuple[str, ...] = (
    "3.0",
    "3.1",
    "3.2",  # LED-290 -- OpenAPI 3.2.0 released 2025
)

# Swagger 2.0 is supported via the legacy `swagger` top-level key.
SUPPORTED_SWAGGER_VERSIONS: Tuple[str, ...] = ("2.0",)

# Human-readable string used in README and tool output.
SUPPORTED_VERSIONS_DISPLAY = "OpenAPI 3.0, 3.1, 3.2 and Swagger 2.0"


def detect_version(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect and validate the version of an OpenAPI / Swagger spec.

    Returns a dict with:
        family:    "openapi" | "swagger" | "unknown"
        version:   the raw version string from the spec, or None
        major_minor: e.g. "3.2" for openapi, "2.0" for swagger, or None
        supported: True if the version is in our allowlist
        warning:   optional human-readable warning if unsupported

    The function is non-throwing -- a missing or weird spec returns
    {family: "unknown", supported: False, ...}. Callers decide whether
    to log/skip/error.
    """
    if not isinstance(spec, dict):
        return {
            "family": "unknown",
            "version": None,
            "major_minor": None,
            "supported": False,
            "warning": "spec is not a mapping",
        }

    raw = spec.get("openapi")
    if isinstance(raw, str) and raw.strip():
        family = "openapi"
        version = raw.strip()
        parts = version.split(".")
        major_minor = ".".join(parts[:2]) if len(parts) >= 2 else version
        supported = major_minor in SUPPORTED_OPENAPI_FAMILIES
        warning = None
        if not supported:
            warning = (
                f"OpenAPI version {version!r} is not in Delimit's validated "
                f"set ({', '.join(SUPPORTED_OPENAPI_FAMILIES)}). The diff "
                f"engine is structurally version-agnostic and will still run, "
                f"but some new keywords may be ignored."
            )
        return {
            "family": family,
            "version": version,
            "major_minor": major_minor,
            "supported": supported,
            "warning": warning,
        }

    raw = spec.get("swagger")
    if isinstance(raw, str) and raw.strip():
        version = raw.strip()
        supported = version in SUPPORTED_SWAGGER_VERSIONS
        warning = None
        if not supported:
            warning = (
                f"Swagger version {version!r} is not supported. "
                f"Delimit supports {', '.join(SUPPORTED_SWAGGER_VERSIONS)}."
            )
        return {
            "family": "swagger",
            "version": version,
            "major_minor": version,
            "supported": supported,
            "warning": warning,
        }

    return {
        "family": "unknown",
        "version": None,
        "major_minor": None,
        "supported": False,
        "warning": "spec has no 'openapi' or 'swagger' top-level version key",
    }


def assert_supported(spec: Dict[str, Any], strict: bool = False) -> Dict[str, Any]:
    """Run version detection and emit a logger warning for unsupported versions.

    When ``strict=True``, raise ValueError instead of warning. Defaults to
    non-strict so existing CI flows are not broken by an unknown version.
    """
    info = detect_version(spec)
    if not info["supported"] and info["warning"]:
        if strict:
            raise ValueError(info["warning"])
        logger.warning(info["warning"])
    return info
