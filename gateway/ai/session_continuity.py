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
_DEFAULT_EPHEMERAL_SOULS_BASE_DIR = EPHEMERAL_SOULS_BASE_DIR
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


def _continuity_home() -> Path:
    """Resolve the same customer-selected state root as the full backend."""
    for env_key in ("DELIMIT_HOME", "DELIMIT_NAMESPACE_ROOT"):
        value = os.environ.get(env_key, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".delimit"


def resolve_project_path(project_path: str = "") -> str:
    """Return one repo-stable identity without ever crossing repositories.

    MCP clients normally launch the server from the active workspace.  Two
    clients can still start in different subdirectories of that workspace, so
    use the nearest git worktree root when available.  A non-git directory is
    isolated by its real path.  There is deliberately no global-most-recent
    fallback: missing project identity must never leak another repo's soul.
    """
    raw = str(project_path or "").strip() or os.getcwd()
    expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    start = Path(expanded)
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        try:
            # A normal repository has a .git directory; a linked worktree has
            # a .git file. The marker is enough to establish the workspace
            # boundary without invoking a user-configurable git executable.
            if (candidate / ".git").exists():
                return os.path.realpath(str(candidate))
        except OSError:
            continue
    return expanded


def _project_hash(project_path: str) -> str:
    """Stable hash for a project path, used as directory name."""
    normalized = resolve_project_path(project_path)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Soul storage directory for a project (ephemeral-guarded like phoenix)."""
    project_path = resolve_project_path(project_path)
    if SOULS_BASE_DIR != _DEFAULT_SOULS_BASE_DIR:
        base = SOULS_BASE_DIR
    elif is_ephemeral_path(project_path):
        base = (
            EPHEMERAL_SOULS_BASE_DIR
            if EPHEMERAL_SOULS_BASE_DIR != _DEFAULT_EPHEMERAL_SOULS_BASE_DIR
            else _continuity_home() / "_ephemeral_scratch" / "souls"
        )
    else:
        base = _continuity_home() / "souls"
    return base / _project_hash(project_path)


def _ensure_private_directory(path: Path) -> None:
    """Create a continuity directory and repair permissive existing modes."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def _prepare_project_dir(project_path: str) -> Path:
    """Create every Delimit-owned continuity directory with owner-only access."""
    project_dir = _project_dir(project_path)
    base = project_dir.parent
    continuity_home = _continuity_home()
    try:
        relative_base = base.relative_to(continuity_home)
    except ValueError:
        # Tests and embedded callers may explicitly replace SOULS_BASE_DIR.
        # The selected base is still Delimit-owned, but its ancestors are not.
        _ensure_private_directory(base)
    else:
        current = continuity_home
        _ensure_private_directory(current)
        for part in relative_base.parts:
            current = current / part
            _ensure_private_directory(current)
    _ensure_private_directory(project_dir)
    if os.name != "nt":
        # Upgrading must repair souls written by older releases, not merely
        # protect the two files produced by the next capture.
        for existing in project_dir.glob("*.json"):
            if not existing.is_symlink():
                existing.chmod(0o600)
    return project_dir


def _write_private_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomically replace a soul file without an ambient-umask exposure window."""
    payload = json.dumps(data, indent=2)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _store_soul_basic(soul: SessionSoul) -> Path:
    """Persist a soul + latest.json pointer, prune to MAX_SOULS_PER_PROJECT.

    Layout contract shared with phoenix._store_soul (parity-tested):
    <base>/<hash12>/<UTCts>_<counter06>_<soul_id>.json + latest.json copy.
    """
    global _capture_counter
    proj_dir = _prepare_project_dir(soul.project_path)
    _capture_counter += 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = proj_dir / f"{ts}_{_capture_counter:06d}_{soul.soul_id}.json"
    data = asdict(soul)
    _write_private_json(filepath, data)
    _write_private_json(proj_dir / "latest.json", data)
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
    resolved_project = resolve_project_path(project_path)
    soul = SessionSoul(
        soul_id=uuid.uuid4().hex[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        source_model=source_model,
        project_path=resolved_project,
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
    """Revive canonical state, or an exact requested-path legacy soul.

    Old Free-core captures hashed the client cwd instead of its repo root.
    When canonical state is absent, recover only that explicitly requested
    path's old hash (cwd when omitted), with matching embedded identity. No
    directory scan, global latest selection, or migration write is performed.
    Capture a new soul to make recovered state available from the repo root.
    """
    project = resolve_project_path(project_path)
    latest = _project_dir(project) / "latest.json"
    legacy_path_recovery = False
    if not latest.exists():
        requested = os.path.realpath(os.path.abspath(os.path.expanduser(
            str(project_path or "").strip() or os.getcwd()
        )))
        legacy_hash = hashlib.sha256(requested.encode()).hexdigest()[:12]
        legacy = latest.parent.parent / legacy_hash / "latest.json"
        if (
            requested == project or legacy.parent.is_symlink()
            or legacy.is_symlink() or not legacy.is_file()
        ):
            return {"status": "not_found", "project": project,
                    "message": "No soul captured for this project yet."}
        latest = legacy
        legacy_path_recovery = True
    try:
        data = json.loads(latest.read_text())
    except (OSError, ValueError) as exc:
        return {"status": "error", "project": project,
                "message": f"soul unreadable: {exc}"}
    if legacy_path_recovery:
        recorded = data.get("project_path") if isinstance(data, dict) else None
        if (
            not isinstance(recorded, str) or not os.path.isabs(recorded)
            or os.path.realpath(recorded) != requested
            or resolve_project_path(recorded) != project
        ):
            return {"status": "not_found", "project": project,
                    "message": "Legacy soul identity did not match the requested project."}
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
            "tier": "core", **({"legacy_path_recovery": True} if legacy_path_recovery else {})}


def revive_legacy_neutral_by_id(soul_id: str) -> Dict[str, Any]:
    """Recover one exact legacy neutral-cwd soul without assigning it to a repo.

    Older MCP launchers pinned the process cwd to ``<DELIMIT_HOME>/server``.
    Those souls are preserved evidence, but their project identity is not
    trustworthy.  An explicit id may recover one unambiguous record; there is
    deliberately no latest/global fallback and no write into the caller's
    current project store.
    """
    requested = str(soul_id or "").strip()
    if not requested or not all(char.isalnum() or char in "-_" for char in requested):
        return {
            "status": "not_found",
            "soul_id": requested,
            "message": "No exact preserved legacy soul matched that id.",
        }

    matches: List[Dict[str, Any]] = []
    # DELIMIT_HOME can move during an upgrade. Search the selected root and
    # the historical default, but only inside the two known soul stores and
    # only for records whose embedded identity is that root's neutral server
    # cwd. This is recovery, not a global soul search.
    roots = {_continuity_home().resolve(), (Path.home() / ".delimit").resolve()}
    for root in roots:
        neutral_project = resolve_project_path(str(root / "server"))
        project_hash = _project_hash(neutral_project)
        for souls_root in (root / "souls", root / "_ephemeral_scratch" / "souls"):
            directory = souls_root / project_hash
            if directory.is_symlink() or not directory.is_dir():
                continue
            for candidate in directory.glob("*.json"):
                if candidate.is_symlink():
                    continue
                try:
                    data = json.loads(candidate.read_text())
                except (OSError, ValueError):
                    continue
                recorded_project = str(data.get("project_path") or "")
                if (
                    data.get("soul_id") == requested
                    and recorded_project
                    and resolve_project_path(recorded_project) == neutral_project
                ):
                    matches.append(data)

    if not matches:
        return {
            "status": "not_found",
            "soul_id": requested,
            "legacy_unmapped": True,
            "message": (
                "No exact preserved legacy soul matched that id; "
                "no fallback was returned."
            ),
        }

    # A normal capture appears once as a timestamped record and once as
    # latest.json. Identical copies are one logical soul; conflicting records
    # sharing an id are unsafe to choose between.
    canonical = {json.dumps(record, sort_keys=True) for record in matches}
    if len(canonical) != 1:
        return {
            "status": "ambiguous",
            "soul_id": requested,
            "legacy_unmapped": True,
            "message": (
                "Conflicting preserved legacy souls share that id; "
                "no context was returned."
            ),
        }

    data = matches[0]
    lines = [
        "## Session Continuity - Preserved Legacy Context (unmapped)",
        f"Soul ID: {requested}  Captured: {data.get('created_at', '?')}",
        "This legacy neutral-cwd soul has not been assigned to the current repository.",
        f"Active task ({data.get('task_status', '?')}): {data.get('active_task', '')}",
    ]
    for label, key in (
        ("Decisions", "decisions"),
        ("Key context", "key_context"),
        ("Blockers", "blockers"),
        ("Next steps", "next_steps"),
    ):
        values = data.get(key) or []
        if values:
            lines.append(f"{label}:")
            lines.extend(f"  - {value}" for value in values)
    return {
        "status": "revived",
        "soul": data,
        "context": "\n".join(lines),
        "tier": "core",
        "legacy_unmapped": True,
    }


# ── graceful public surface: full phoenix when present, core otherwise ─────


def _session_phoenix_is_absent(exc: BaseException) -> bool:
    """Return true only when the optional Phoenix module itself is absent.

    Import failures raised by a dependency *inside* Session Phoenix are real
    installation defects and must propagate instead of silently selecting the
    Free fallback.
    """
    return (
        isinstance(exc, ModuleNotFoundError)
        and exc.name in {"ai.session_phoenix", "session_phoenix"}
    )


def capture_soul_best(**kwargs: Any) -> Dict[str, Any]:
    """Capture via phoenix when installed; core standalone otherwise.

    Never hard-errors on a missing backend (ratified condition 3)."""
    try:
        from ai.session_phoenix import capture_soul as _full
        return _full(**kwargs)
    except ModuleNotFoundError as exc:
        if not _session_phoenix_is_absent(exc):
            raise
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
    except ModuleNotFoundError as exc:
        if not _session_phoenix_is_absent(exc):
            raise
        return revive_basic(project_path=project_path)
