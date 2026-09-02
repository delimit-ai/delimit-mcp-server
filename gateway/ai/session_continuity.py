"""Public session-continuity core — the free-tier capture/revive contract.

FOUNDER-RATIFIED boundary (STR-3724 item 1, Option B, 2026-08-08; deliberation
2026-08-08-led4063-continuity-packaging-boundary.md, unanimous 4/4 vendors):
this module is the STABLE FREE-CORE CONTRACT that ships in the public npm
bundle so fresh installs' session-start ritual (delimit_revive) and Delimit
Chat work out of the box. The differentiating phoenix internals — cross-
harness orphan-transcript discovery, git-state forensics, pruning policy
richness, corpus integration — live in ai/session_phoenix.py, which is
EXCLUDED from npm (bundle-internal-exclude.txt) and upgrades this surface in
place when present.

Binding conditions from the ratification (enforced by tests in the same PR):
  1. ONE shared schema: ``SessionSoul`` is defined HERE and phoenix imports
     it — the backend consumes the schema, never forks it.
  2. Parity: a soul written by this module's standalone store is revivable by
     phoenix and vice versa (identical directory layout + latest.json
     contract), pinned by contract tests.
  3. Graceful degradation: the public entrypoints below NEVER hard-error on a
     missing backend — full phoenix when importable, standalone otherwise.

Standalone behavior is deliberately minimal: explicit-argument capture (no
git detection), latest-soul revive with a formatted context block. That is
the advertised out-of-box loop; everything richer is the Pro upgrade.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Same ephemeral-path guard phoenix uses (single source; do not fork). On a
# minimal npm layout where registry_guards is absent, fall back to "never
# ephemeral" — the guard only protects local test runs, not production.
try:
    from ai.registry_guards import is_ephemeral_path
except ImportError:  # pragma: no cover - flat / minimal bundle layout
    try:
        from registry_guards import is_ephemeral_path  # type: ignore
    except ImportError:
        def is_ephemeral_path(_p: str) -> bool:  # type: ignore
            return False

MAX_SOULS_PER_PROJECT = 10
SOULS_BASE_DIR = Path.home() / ".delimit" / "souls"
_DEFAULT_SOULS_BASE_DIR = SOULS_BASE_DIR
EPHEMERAL_SOULS_BASE_DIR = Path.home() / ".delimit" / "_ephemeral_scratch" / "souls"
_capture_counter = 0


@dataclass
class SessionSoul:
    """Compressed session state that survives death.

    THE shared soul schema (ratified condition 1): phoenix imports this class
    from here. Field additions must be backward-compatible (defaults only) —
    customer soul files exist on disk (never-break-installs).
    """

    soul_id: str = ""
    created_at: str = ""
    source_model: str = "unknown"
    project_path: str = ""

    # What was being worked on
    active_task: str = ""
    task_status: str = "in_progress"  # in_progress, blocked, almost_done

    # Key decisions made this session
    decisions: List[str] = field(default_factory=list)

    # Files touched
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)

    # Context that matters
    key_context: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)

    # Technical state
    git_branch: str = ""
    git_sha: str = ""
    uncommitted_changes: int = 0

    # Token stats
    tokens_used: int = 0
    context_fullness: float = 0.0

    # Deterministic continuity-floor provenance (LED-4057). Additive fields
    # preserve compatibility with all legacy soul files (defaults only —
    # customer soul JSONs on disk deserialize unchanged).
    updated_at: str = ""
    venture: str = ""
    transcript_path: str = ""
    transcript_id: str = ""
    transcript_size: int = 0
    transcript_mtime_ns: int = 0
    transcript_tail_sha256: str = ""
    logical_session_id: str = ""
    chat_session_id: str = ""
    capture_key: str = ""
    handoff_id: str = ""
    references: List[str] = field(default_factory=list)
    background_tasks: List[Dict[str, Any]] = field(default_factory=list)
    launcher_run_id: str = ""
    lifecycle_status: str = ""


# ── standalone store (layout-identical to phoenix; parity-tested) ──────────


def _project_hash(project_path: str) -> str:
    """Stable hash for a project path, used as directory name."""
    normalized = os.path.realpath(project_path)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Soul storage directory for a project (ephemeral-guarded like phoenix)."""
    if SOULS_BASE_DIR != _DEFAULT_SOULS_BASE_DIR:
        base = SOULS_BASE_DIR
    elif is_ephemeral_path(project_path):
        base = EPHEMERAL_SOULS_BASE_DIR
    else:
        base = SOULS_BASE_DIR
    return base / _project_hash(project_path)


def _store_soul_basic(soul: SessionSoul) -> Path:
    """Persist a soul + latest.json pointer, prune to MAX_SOULS_PER_PROJECT.

    Layout contract shared with phoenix._store_soul (parity-tested):
    <base>/<hash12>/<UTCts>_<counter06>_<soul_id>.json + latest.json copy.
    """
    global _capture_counter
    proj_dir = _project_dir(soul.project_path)
    proj_dir.mkdir(parents=True, exist_ok=True)
    _capture_counter += 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = proj_dir / f"{ts}_{_capture_counter:06d}_{soul.soul_id}.json"
    data = asdict(soul)
    filepath.write_text(json.dumps(data, indent=2))
    (proj_dir / "latest.json").write_text(json.dumps(data, indent=2))
    soul_files = sorted(p for p in proj_dir.glob("*.json") if p.name != "latest.json")
    while len(soul_files) > MAX_SOULS_PER_PROJECT:
        soul_files.pop(0).unlink(missing_ok=True)
    return filepath


def capture_soul_core(
    project_path: str = "",
    active_task: str = "",
    task_status: str = "in_progress",
    decisions: List[str] | None = None,
    key_context: List[str] | None = None,
    blockers: List[str] | None = None,
    next_steps: List[str] | None = None,
    source_model: str = "unknown",
    tokens_used: int = 0,
    context_fullness: float = 0.0,
) -> SessionSoul:
    """Minimal capture returning the STORED ``SessionSoul`` — the same return
    shape as phoenix's ``capture_soul``, so the server tools' fallback path is
    a drop-in (the handler attribute-accesses the result; returning a dict
    here would AttributeError on fresh installs — the exact hard-error the
    ratified graceful-degradation condition forbids)."""
    soul = SessionSoul(
        soul_id=uuid.uuid4().hex[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        source_model=source_model,
        project_path=project_path or os.getcwd(),
        active_task=active_task,
        task_status=task_status,
        decisions=list(decisions or []),
        key_context=list(key_context or []),
        blockers=list(blockers or []),
        next_steps=list(next_steps or []),
        tokens_used=int(tokens_used or 0),
        context_fullness=float(context_fullness or 0.0),
    )
    _store_soul_basic(soul)
    return soul


def capture_soul_basic(**kwargs: Any) -> Dict[str, Any]:
    """Dict-summary convenience wrapper over :func:`capture_soul_core`."""
    soul = capture_soul_core(**kwargs)
    return {
        "status": "captured",
        "soul_id": soul.soul_id,
        "project": soul.project_path,
        "tier": "core",
        "message": f"Soul {soul.soul_id} captured (core continuity).",
    }


def revive_basic(project_path: str = "") -> Dict[str, Any]:
    """Minimal revive: latest soul for the project + formatted context."""
    project = project_path or os.getcwd()
    latest = _project_dir(project) / "latest.json"
    if not latest.exists():
        return {"status": "not_found", "project": project,
                "message": "No soul captured for this project yet."}
    try:
        data = json.loads(latest.read_text())
    except (OSError, ValueError) as exc:
        return {"status": "error", "project": project,
                "message": f"soul unreadable: {exc}"}
    lines = [
        "## Session Continuity — Revived Context (core)",
        f"Soul ID: {data.get('soul_id', '?')}  Captured: {data.get('created_at', '?')}",
        f"Active task ({data.get('task_status', '?')}): {data.get('active_task', '')}",
    ]
    for label, key in (("Decisions", "decisions"), ("Key context", "key_context"),
                       ("Blockers", "blockers"), ("Next steps", "next_steps")):
        vals = data.get(key) or []
        if vals:
            lines.append(f"{label}:")
            lines.extend(f"  - {v}" for v in vals)
    return {"status": "revived", "soul": data, "context": "\n".join(lines),
            "tier": "core"}


# ── graceful public surface: full phoenix when present, core otherwise ─────


def capture_soul_best(**kwargs: Any) -> Dict[str, Any]:
    """Capture via phoenix when installed; core standalone otherwise.

    Never hard-errors on a missing backend (ratified condition 3)."""
    try:
        from ai.session_phoenix import capture_soul as _full
        return _full(**kwargs)
    except ImportError:
        _allowed = capture_soul_core.__code__.co_varnames[
            : capture_soul_core.__code__.co_argcount
        ]
        return capture_soul_basic(**{
            k: v for k, v in kwargs.items() if k in _allowed
        })


def revive_best(project_path: str = "", **kwargs: Any) -> Dict[str, Any]:
    """Revive via phoenix when installed; core standalone otherwise."""
    try:
        from ai.session_phoenix import revive as _full
        return _full(project_path=project_path, **kwargs)
    except ImportError:
        return revive_basic(project_path=project_path)
