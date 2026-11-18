"""Which security-audit targets may auto-file ledger items.

The security audit files a P0 ledger item (and a governance task and a
notification) whenever a scanned tree has critical findings or vulnerable
dependencies. Agents also run the audit as a gate on disposable copies of a
repository: agent worktrees, immutable release snapshots and scratch checkouts.
Each copy has its own path, so one codebase filed one P0 per copy (2026-11
triage: 18 open P0s, most of them copies of two repositories).

This module decides whether a target is in such a disposable location. It
never changes what the audit finds: findings, severity counts, evidence and
the deploy-plan block are untouched. Only the ledger, governance-task and
notification side effects are skipped, and the result says why.

A target is out of ledger scope only when its resolved path matches a glob
in ``ledger_exclude_paths`` of ``<DELIMIT_HOME>/security_audit.json``, for
example::

      {"ledger_exclude_paths": ["/home/agent-worktrees/*", "/home/releases/*"]}

  ``*`` matches across ``/``, so ``/a/*`` also covers ``/a/b/c``. A glob
  without a wildcard also covers everything below it.

There are no built-in exclusions: with no config file, behaviour is exactly
as before (every target files). Fails safe toward filing: an unreadable or
malformed config means no exclusions.
"""

from __future__ import annotations

import fnmatch
import json
import os
from typing import List, Optional

__all__ = ["ledger_exclusion_reason", "configured_exclude_paths", "CONFIG_FILENAME"]

CONFIG_FILENAME = "security_audit.json"


def _config_path() -> str:
    try:
        from ai.ledger_manager import _delimit_home

        return str(_delimit_home() / CONFIG_FILENAME)
    except Exception:  # noqa: BLE001 - fail safe: no config
        return ""


def configured_exclude_paths() -> List[str]:
    """Globs from ``ledger_exclude_paths``; empty on any read or parse problem."""
    path = _config_path()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return []
    globs = data.get("ledger_exclude_paths") if isinstance(data, dict) else None
    if not isinstance(globs, list):
        return []
    return [g.strip() for g in globs if isinstance(g, str) and g.strip()]


def _resolve(target: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(target or ".")))
    except Exception:  # noqa: BLE001
        return target or ""


def _matches(path: str, glob: str) -> bool:
    glob = os.path.expanduser(glob).rstrip("/") or "/"
    if fnmatch.fnmatchcase(path, glob):
        return True
    # A plain directory (no wildcard) covers its whole subtree.
    if not any(ch in glob for ch in "*?["):
        return path == glob or path.startswith(glob + "/")
    return False


def ledger_exclusion_reason(target: str) -> Optional[str]:
    """Return why ``target`` must not auto-file ledger items, or None to file.

    Matches the resolved path, so a relative ``.`` and a symlink into an
    excluded tree are judged by where they really point.
    """
    globs = configured_exclude_paths()
    if not globs:
        return None
    resolved = _resolve(target)
    for glob in globs:
        if _matches(resolved, glob):
            return f"config:{glob}"
    return None
