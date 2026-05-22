"""LED-2087 Phase 1a — proprietary-module compilation status helper.

Customers can run on the Python source fallback (slower; no compiled-
attestation parity) when the platform-appropriate ``.so`` /``.pyd``
isn't shipped in their bundle. The 3 proprietary modules
(``license_core``, ``deliberation``, ``governance``) each get this
treatment via the LED-1259 warn-and-fallback path for ``license_core``
and (post-LED-2087-phase-1a) the same introspection here for the
other two.

This module is intentionally minimal-surface:
- No modification of the proprietary source modules
- No new MCP tool added to the customer-facing surface
- One INFO-level log line at gateway startup (silent on the happy
  Linux x86_64 / py3.10 path where all three are native-loaded)
- Importable status helper for tests + future replay tooling
"""
from __future__ import annotations

import importlib
import logging
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("delimit.ai._compile_status")

# Modules audited by this helper. Each tuple is (import_name, friendly_label).
# ai.license_core already has the LED-1259 warn-and-fallback path in
# ai/license.py — we still introspect it here for the consolidated
# startup report.
PROPRIETARY_MODULES: Tuple[Tuple[str, str], ...] = (
    ("ai.license_core", "license_core"),
    ("ai.deliberation", "deliberation"),
    ("ai.governance", "governance"),
)

# Extensions Python uses for native compiled modules across platforms.
# .so   — Linux + macOS
# .pyd  — Windows
# .dylib — macOS dynamic libs (Python typically uses .so on macOS too,
#         but include for defensive coverage)
_NATIVE_EXTS: Tuple[str, ...] = (".so", ".pyd", ".dylib")


def is_native_compiled(import_name: str) -> Optional[bool]:
    """Return True if the named module loaded from a native binary
    (``.so`` / ``.pyd`` / ``.dylib``), False if it loaded from
    ``.py`` source, or None if the module isn't importable at all.

    Pure introspection — no side effects beyond the import call itself.
    The import is harmless: if the module is already imported (almost
    always true at gateway startup) ``importlib`` returns the cached
    module without re-executing.
    """
    try:
        mod = importlib.import_module(import_name)
    except ImportError:
        return None
    path = getattr(mod, "__file__", "") or ""
    if not path:
        # Some build pipelines produce modules without ``__file__``
        # (e.g. frozen modules). Conservatively treat as "unknown but
        # importable" — surface as None so callers don't assume either
        # native or source state.
        return None
    for ext in _NATIVE_EXTS:
        if path.endswith(ext):
            return True
    if path.endswith(".py"):
        return False
    # Unknown extension (e.g. .pyc) — conservatively treat as not-native.
    return False


def compilation_status_report(
    modules: Iterable[Tuple[str, str]] = PROPRIETARY_MODULES,
) -> Dict[str, str]:
    """Return ``{friendly_label: status}`` for each module.

    Status values:
      - ``"native"``  — loaded from .so / .pyd / .dylib
      - ``"source"``  — loaded from .py (fallback path)
      - ``"missing"`` — module not importable at all
      - ``"unknown"`` — importable but ``__file__`` unrecognized

    Used by the startup logger + tests + future status-query tooling.
    Deterministic given the same set of imported modules + the same
    platform (no clock-dependent state).
    """
    report: Dict[str, str] = {}
    for import_name, label in modules:
        native = is_native_compiled(import_name)
        if native is True:
            report[label] = "native"
        elif native is False:
            # Distinguish source-known-extension from unknown.
            try:
                mod = importlib.import_module(import_name)
                path = getattr(mod, "__file__", "") or ""
                report[label] = "source" if path.endswith(".py") else "unknown"
            except ImportError:
                # Should not happen post-is_native_compiled returning False,
                # but cover the race anyway.
                report[label] = "missing"
        else:
            report[label] = "missing"
    return report


def log_compilation_status_on_startup(
    modules: Iterable[Tuple[str, str]] = PROPRIETARY_MODULES,
) -> None:
    """Emit one log line summarizing the proprietary-module load state.

    Silent on the happy path (Linux x86_64 / Python 3.10 dev box where
    all three modules are native-loaded — actually emits INFO, but
    the message clearly says "all native" so ops can scan past).

    Calls this exactly once at gateway server startup. Idempotent: if
    a future caller invokes it twice, both emissions show the same
    state because is_native_compiled is pure.
    """
    report = compilation_status_report(modules)
    native = [label for label, status in report.items() if status == "native"]
    source = [label for label, status in report.items() if status == "source"]
    missing = [label for label, status in report.items() if status == "missing"]
    unknown = [label for label, status in report.items() if status == "unknown"]

    if not source and not missing and not unknown:
        logger.info(
            "[LED-2087] proprietary modules native-loaded: %s",
            ", ".join(native) if native else "(none)",
        )
        return

    # Customer-facing: ANY non-native module gets surfaced clearly so the
    # operator knows performance / attestation parity is degraded for
    # those specific modules. This is the LED-1259 warn-and-fallback
    # pattern extended to deliberation + governance.
    fragments: List[str] = []
    if native:
        fragments.append(f"native={','.join(native)}")
    if source:
        fragments.append(f"source-fallback={','.join(source)}")
    if missing:
        fragments.append(f"missing={','.join(missing)}")
    if unknown:
        fragments.append(f"unknown={','.join(unknown)}")
    logger.warning(
        "[LED-2087] proprietary-module load state — %s. Source-fallback "
        "and missing modules run Python source path (slower; no compiled-"
        "attestation parity). Cross-platform binaries land per the "
        "LED-2087 Phase 1 build matrix.",
        " ".join(fragments),
    )
