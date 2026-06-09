"""
Session Phoenix — Cross-model session resurrection (LED-218).

When a session dies from rate limits, context overflow, or model switch,
the user runs `delimit revive` in any model to restore working state.

Architecture:
  capture_soul() -> ~/.delimit/souls/{project_hash}/{timestamp}.json
  revive()       -> structured context blob any AI model can read

Complements delimit_session_handoff (ledger state) by saving the
working context: task, decisions, files, blockers, next steps.
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_SOULS_PER_PROJECT = 10
SOULS_BASE_DIR = Path.home() / ".delimit" / "souls"
_capture_counter = 0  # Monotonic counter for sub-second ordering


@dataclass
class SessionSoul:
    """Compressed session state that survives death."""

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


def _project_hash(project_path: str) -> str:
    """Stable hash for a project path, used as directory name."""
    normalized = os.path.realpath(project_path)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Return the soul storage directory for a project."""
    return SOULS_BASE_DIR / _project_hash(project_path)


def _run_git(args: List[str], cwd: str = "") -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _detect_git_state(project_path: str) -> Dict[str, Any]:
    """Auto-detect git branch, sha, modified/created files, uncommitted count."""
    cwd = project_path or os.getcwd()

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    sha = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)

    # Uncommitted changes (staged + unstaged + untracked)
    porcelain = _run_git(["status", "--porcelain"], cwd=cwd)
    porcelain_lines = [l for l in porcelain.splitlines() if l.strip()] if porcelain else []
    uncommitted = len(porcelain_lines)

    # Files modified (tracked, staged or unstaged)
    diff_names = _run_git(["diff", "--name-only", "HEAD"], cwd=cwd)
    files_modified = [l.strip() for l in diff_names.splitlines() if l.strip()] if diff_names else []

    # New untracked files
    untracked_raw = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd)
    files_created = [l.strip() for l in untracked_raw.splitlines() if l.strip()] if untracked_raw else []

    return {
        "git_branch": branch,
        "git_sha": sha,
        "uncommitted_changes": uncommitted,
        "files_modified": files_modified,
        "files_created": files_created,
    }


def capture_soul(
    active_task: str = "",
    decisions: Optional[List[str]] = None,
    key_context: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    next_steps: Optional[List[str]] = None,
    source_model: str = "unknown",
    project_path: str = "",
    task_status: str = "in_progress",
    tokens_used: int = 0,
    context_fullness: float = 0.0,
) -> SessionSoul:
    """Capture current session state as a soul and persist it to disk."""
    project_path = project_path or os.getcwd()
    git_state = _detect_git_state(project_path)

    soul = SessionSoul(
        soul_id=str(uuid.uuid4())[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        source_model=source_model,
        project_path=project_path,
        active_task=active_task,
        task_status=task_status,
        decisions=decisions or [],
        files_modified=git_state["files_modified"],
        files_created=git_state["files_created"],
        key_context=key_context or [],
        blockers=blockers or [],
        next_steps=next_steps or [],
        git_branch=git_state["git_branch"],
        git_sha=git_state["git_sha"],
        uncommitted_changes=git_state["uncommitted_changes"],
        tokens_used=tokens_used,
        context_fullness=context_fullness,
    )

    _store_soul(soul)

    # LED-1705: stamp the deterministic-floor coordinator so the Stop hook
    # knows a fresh, richer model capture exists and skips clobbering it.
    try:
        from ai.last_capture import stamp_capture
    except ImportError:  # pragma: no cover - flat import layout
        from last_capture import stamp_capture
    stamp_capture(source="model", session_id=soul.soul_id)

    return soul


def _store_soul(soul: SessionSoul) -> Path:
    """Persist a soul to disk and maintain the latest pointer."""
    global _capture_counter
    proj_dir = _project_dir(soul.project_path)
    proj_dir.mkdir(parents=True, exist_ok=True)

    # Timestamp + monotonic counter for correct ordering within same second
    _capture_counter += 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{ts}_{_capture_counter:06d}_{soul.soul_id}.json"
    filepath = proj_dir / filename

    data = asdict(soul)
    filepath.write_text(json.dumps(data, indent=2))

    # Update latest.json as a copy (symlinks can be fragile across systems)
    latest = proj_dir / "latest.json"
    latest.write_text(json.dumps(data, indent=2))

    # Auto-prune to MAX_SOULS_PER_PROJECT (keep latest N by name sort)
    _prune_souls(proj_dir)

    return filepath


def _prune_souls(proj_dir: Path) -> None:
    """Keep only the latest MAX_SOULS_PER_PROJECT souls per project."""
    soul_files = sorted(
        [f for f in proj_dir.iterdir() if f.name != "latest.json" and f.suffix == ".json"],
        key=lambda f: f.name,
    )
    while len(soul_files) > MAX_SOULS_PER_PROJECT:
        oldest = soul_files.pop(0)
        oldest.unlink(missing_ok=True)


def _load_soul(path: Path) -> Optional[SessionSoul]:
    """Load a soul from a JSON file."""
    try:
        data = json.loads(path.read_text())
        return SessionSoul(**{k: v for k, v in data.items() if k in SessionSoul.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError, KeyError, OSError):
        return None


def list_souls(project_path: str = "") -> List[SessionSoul]:
    """List all stored souls for a project, newest first."""
    project_path = project_path or os.getcwd()
    proj_dir = _project_dir(project_path)
    if not proj_dir.exists():
        return []

    soul_files = sorted(
        [f for f in proj_dir.iterdir() if f.name != "latest.json" and f.suffix == ".json"],
        key=lambda f: f.name,
        reverse=True,
    )
    souls = []
    for f in soul_files:
        soul = _load_soul(f)
        if soul:
            souls.append(soul)
    return souls


def get_latest_soul(project_path: str = "") -> Optional[SessionSoul]:
    """Get the most recent soul for a project."""
    project_path = project_path or os.getcwd()
    latest = _project_dir(project_path) / "latest.json"
    if latest.exists():
        return _load_soul(latest)
    return None


def _soul_sort_key(soul: SessionSoul, fallback_path: Path) -> str:
    """Sort key for global recency ranking. Prefer the soul's own
    created_at (ISO-8601, lexically sortable); fall back to the file's
    mtime when created_at is missing so a malformed/legacy soul still
    orders sensibly rather than sinking to the bottom unconditionally."""
    if soul.created_at:
        return soul.created_at
    try:
        # Fall back to the file mtime, rendered as an ISO-8601 string so it
        # compares lexically against real created_at values on the same
        # scale. Only reached when created_at is empty.
        return datetime.fromtimestamp(
            fallback_path.stat().st_mtime, timezone.utc
        ).isoformat()
    except (OSError, ValueError):
        return ""


def find_most_recent_soul_across_projects(
    exclude_project_path: str = "",
) -> Optional[Dict[str, Any]]:
    """Scan every project-hash soul directory under SOULS_BASE_DIR and
    return the globally-most-recent soul, with its originating project.

    LED-218 FIX D: cross-venture fallback for `revive()` when the current
    working directory resolves to a project that has no souls (e.g. running
    from /root). Read-only; never writes. Returns None when no souls exist
    anywhere.

    Args:
        exclude_project_path: if set, the soul directory for this project
            is skipped (it already had no usable soul, so re-scanning it is
            wasted work and could otherwise re-surface a stale latest.json).

    Returns:
        {"soul": SessionSoul, "project_hash": str, "project_path": str}
        for the most recent soul found, or None.
    """
    if not SOULS_BASE_DIR.exists():
        return None

    exclude_hash = _project_hash(exclude_project_path) if exclude_project_path else None

    best: Optional[SessionSoul] = None
    best_key: str = ""
    best_hash: str = ""

    for proj_dir in SOULS_BASE_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        if exclude_hash and proj_dir.name == exclude_hash:
            continue

        # Prefer the per-project latest.json; fall back to scanning the
        # timestamped soul files if latest.json is absent/corrupt.
        candidate: Optional[SessionSoul] = None
        candidate_path: Optional[Path] = None

        latest = proj_dir / "latest.json"
        if latest.exists():
            candidate = _load_soul(latest)
            candidate_path = latest

        if candidate is None:
            soul_files = sorted(
                [f for f in proj_dir.iterdir()
                 if f.name != "latest.json" and f.suffix == ".json"],
                key=lambda f: f.name,
                reverse=True,
            )
            for f in soul_files:
                candidate = _load_soul(f)
                if candidate is not None:
                    candidate_path = f
                    break

        if candidate is None or candidate_path is None:
            continue

        key = _soul_sort_key(candidate, candidate_path)
        if best is None or key > best_key:
            best = candidate
            best_key = key
            best_hash = proj_dir.name

    if best is None:
        return None

    return {
        "soul": best,
        "project_hash": best_hash,
        "project_path": best.project_path,
    }


def _format_revival(soul: SessionSoul) -> str:
    """Format a soul into a readable context string for any AI model.

    Uses Markdown headers (not long ``=``/``-`` rune runs or ``--- SECTION ---``
    markers) so the formatted output does not trip delimit's own
    prompt-injection / delimiter-manipulation detector when re-ingested.
    """
    lines = []
    lines.append("## Session Phoenix — Revived Context")
    lines.append("")

    lines.append(f"Soul ID:      {soul.soul_id}")
    lines.append(f"Captured:     {soul.created_at}")
    lines.append(f"Source Model: {soul.source_model}")
    lines.append(f"Project:      {soul.project_path}")
    lines.append("")

    # Current task
    lines.append("### Active Task")
    if soul.active_task:
        lines.append(f"  {soul.active_task}")
        lines.append(f"  Status: {soul.task_status}")
    else:
        lines.append("  (none recorded)")
    lines.append("")

    # Decisions
    if soul.decisions:
        lines.append("### Key Decisions")
        for d in soul.decisions:
            lines.append(f"  - {d}")
        lines.append("")

    # Files
    if soul.files_modified or soul.files_created:
        lines.append("### Files Changed")
        for f in soul.files_modified:
            lines.append(f"  M {f}")
        for f in soul.files_created:
            lines.append(f"  + {f}")
        lines.append("")

    # Context
    if soul.key_context:
        lines.append("### Key Context")
        for c in soul.key_context:
            lines.append(f"  - {c}")
        lines.append("")

    # Blockers
    if soul.blockers:
        lines.append("### Blockers")
        for b in soul.blockers:
            lines.append(f"  ! {b}")
        lines.append("")

    # Next steps
    if soul.next_steps:
        lines.append("### Next Steps")
        for i, s in enumerate(soul.next_steps, 1):
            lines.append(f"  {i}. {s}")
        lines.append("")

    # Git state
    lines.append("### Git State")
    lines.append(f"  Branch: {soul.git_branch or '(unknown)'}")
    lines.append(f"  SHA:    {soul.git_sha or '(unknown)'}")
    lines.append(f"  Uncommitted changes: {soul.uncommitted_changes}")
    lines.append("")

    # Token stats
    if soul.tokens_used or soul.context_fullness:
        lines.append("### Session Stats")
        if soul.tokens_used:
            lines.append(f"  Tokens used: ~{soul.tokens_used:,}")
        if soul.context_fullness:
            lines.append(f"  Context fullness: {soul.context_fullness:.0%}")
        lines.append("")

    return "\n".join(lines)


def _reconcile_orphaned_session(
    project_path: str, transcript_path: str = ""
) -> Optional[Dict[str, Any]]:
    """LED-1705 startup-side crash-gap reconciliation.

    A clean session exit stamps ``~/.delimit/.last_capture``. A hard crash /
    SIGKILL never fires the Stop hook, so the previous session leaves NO
    stamp (orphaned). On the NEXT revive we detect that gap and salvage a
    cheap ``source="deterministic", quality="floor"`` handoff from the
    orphaned transcript so the crashed session's tail isn't lost.

    Best-effort and CHEAP: git state + transcript-tail parse only, no LLM.
    Never raises, never blocks revive. Returns the salvaged handoff dict on
    success, or ``None`` when there's nothing to salvage (stamp present, no
    transcript, or any error).
    """
    try:
        from ai.last_capture import (
            read_last_capture,
            parse_transcript_tail,
            stamp_capture,
        )
    except ImportError:  # pragma: no cover - flat import layout
        from last_capture import (  # type: ignore
            read_last_capture,
            parse_transcript_tail,
            stamp_capture,
        )

    # If a stamp exists, the previous session ended cleanly (model OR
    # deterministic floor) — nothing orphaned to salvage.
    if read_last_capture() is not None:
        return None
    if not transcript_path:
        return None

    tail = parse_transcript_tail(transcript_path)
    if not tail.get("turns"):
        return None

    git_state = _detect_git_state(project_path)
    summary = tail.get("final_assistant_text", "") or "(orphaned session — no final text)"
    next_steps = []
    if tail.get("tool_calls"):
        next_steps.append(
            "Last tool activity: " + ", ".join(tail["tool_calls"][-10:])
        )

    try:
        try:
            from ai.ledger_manager import session_handoff
        except ImportError:  # pragma: no cover
            from ledger_manager import session_handoff  # type: ignore
        handoff = session_handoff(
            summary="[deterministic floor / orphaned] " + summary[:2000],
            files_changed=git_state.get("files_modified", []),
            key_decisions=next_steps,
        )
    except Exception:
        handoff = {"summary": summary, "salvaged": True}

    # The session_handoff above stamps source="model"; overwrite with an
    # honest deterministic-floor stamp so freshness logic isn't fooled.
    stamp_capture(
        source="deterministic",
        session_id=str(handoff.get("saved", "")),
        quality="floor",
    )
    return {"salvaged_handoff": handoff, "transcript_turns": tail.get("turns", 0)}


def _find_orphan_transcript(current_transcript_path: str = "") -> str:
    """Locate the most-recent prior-session transcript JSONL to salvage.

    Claude Code stores per-project transcripts as ``<session_uuid>.jsonl`` in
    the project directory (the parent of ``current_transcript_path``). At
    SessionStart the NEWEST transcript is the CURRENT session's, so we must
    exclude it and return the next-most-recent one — that's the candidate
    orphan (the session that may have been SIGKILLed without a clean Stop).

    Returns the orphan transcript path, or "" when none can be determined
    (no current path, no sibling transcripts, or any error). Cheap + best
    effort: a single directory listing, no parsing.
    """
    try:
        if not current_transcript_path:
            return ""
        cur = Path(current_transcript_path)
        proj_dir = cur.parent
        if not proj_dir.is_dir():
            return ""
        cur_real = os.path.realpath(str(cur))
        candidates = [
            f for f in proj_dir.glob("*.jsonl")
            if os.path.realpath(str(f)) != cur_real
        ]
        if not candidates:
            return ""
        # Newest by mtime among the non-current transcripts.
        newest = max(candidates, key=lambda f: f.stat().st_mtime)
        return str(newest)
    except Exception:
        return ""


def reconcile_orphan(
    transcript_path: str = "",
    project_path: str = "",
    current_transcript_path: str = "",
) -> Dict[str, Any]:
    """Public SessionStart entry point for crash-gap reconciliation (LED-1705).

    Called from the SessionStart hook. If the previous session ended cleanly
    (a fresh ``.last_capture`` stamp exists) this is a no-op. Otherwise it
    salvages a cheap ``source="deterministic", quality="floor"`` handoff from
    the orphaned prior-session transcript so a SIGKILL/crash doesn't lose the
    crashed session's tail.

    Args:
        transcript_path: explicit orphan transcript to salvage. When empty,
            the orphan is auto-discovered from ``current_transcript_path``'s
            project directory (newest sibling that isn't the current one).
        project_path: repo path for cheap git-state capture; defaults to cwd.
        current_transcript_path: the CURRENT session's transcript, used to
            exclude it during auto-discovery (the newest transcript at
            SessionStart is always the current session's, not the orphan).

    Returns a small status dict. Best-effort; never raises.
    """
    project_path = project_path or os.getcwd()
    try:
        from ai.last_capture import read_last_capture
    except ImportError:  # pragma: no cover - flat import layout
        from last_capture import read_last_capture  # type: ignore

    # Double-reconcile guard: a stamp means the prior session was captured
    # (cleanly OR by a previous reconcile run), so there's nothing to salvage.
    if read_last_capture() is not None:
        return {"status": "skipped", "reason": "last_capture_present"}

    orphan = transcript_path or _find_orphan_transcript(current_transcript_path)
    if not orphan:
        return {"status": "skipped", "reason": "no_orphan_transcript"}

    result = _reconcile_orphaned_session(project_path, orphan)
    if result is None:
        return {"status": "skipped", "reason": "nothing_to_salvage", "orphan": orphan}
    return {"status": "reconciled", "orphan": orphan, **result}


def revive(
    project_path: str = "",
    soul_id: str = "",
    scope: str = "",
    transcript_path: str = "",
) -> Dict[str, Any]:
    """Revive the latest session soul for this project.

    Returns a structured dict with both the raw soul data and a
    formatted context string that can be injected into any model.

    When ``scope`` is non-empty it is treated as a handoff/receipt id: the
    scoped handoff context is revived INSTEAD of the global session soul.
    This lets a dispatched subagent revive only its own handoff context and
    avoid pulling in the orchestrator's global ``active_task`` (scope-bleed).
    A scoped miss returns ``not_found`` and never falls back to the global
    soul (falling back would defeat the isolation). ``scope=""`` preserves
    today's exact behavior (full soul, including the cross-venture fallback).

    LED-1705: when ``transcript_path`` is supplied AND the previous session
    left no ``.last_capture`` stamp (crash / SIGKILL — the Stop hook never
    fired), a cheap deterministic floor handoff is salvaged from that
    orphaned transcript before reviving. Best-effort; never blocks revive.
    """
    if scope:
        # Scoped subagent revival — load the named handoff receipt only.
        try:
            from ai.handoff_receipts import get_receipt, format_receipt
        except ImportError:  # pragma: no cover - support flat import layout
            from handoff_receipts import get_receipt, format_receipt
        receipt = get_receipt(scope, project_path=project_path)
        if receipt is None:
            return {
                "status": "not_found",
                "scope": scope,
                "message": (
                    f"No handoff receipt with id '{scope}' found. "
                    "Scoped revival does not fall back to the global soul."
                ),
            }
        from dataclasses import asdict as _asdict
        return {
            "status": "revived_scoped",
            "handoff": _asdict(receipt),
            "context": format_receipt(receipt),
            "scope": scope,
        }

    project_path = project_path or os.getcwd()

    if soul_id:
        # Search for a specific soul by ID
        for soul in list_souls(project_path):
            if soul.soul_id == soul_id:
                return {
                    "status": "revived",
                    "soul": asdict(soul),
                    "context": _format_revival(soul),
                }
        return {
            "status": "not_found",
            "message": f"No soul with ID '{soul_id}' found for project {project_path}",
        }

    # LED-1705: close the crash gap. If the previous session was SIGKILLed
    # (Stop hook never fired -> no .last_capture stamp), salvage a cheap
    # deterministic floor handoff from the orphaned transcript. Best-effort;
    # never blocks revive. The salvage result is surfaced under
    # `reconciled_orphan` so the caller can see continuity was recovered.
    reconciled = None
    try:
        reconciled = _reconcile_orphaned_session(project_path, transcript_path)
    except Exception:
        reconciled = None

    # Get latest
    soul = get_latest_soul(project_path)
    if not soul:
        # FIX D — cross-venture fallback. The current working directory
        # resolved to a project with no soul (common when reviving from a
        # neutral dir like /root). Rather than dead-ending at "no_souls",
        # surface the globally-most-recent soul from any other venture /
        # project so the operator still gets continuity. Clearly labeled
        # via `recovered_from_venture` so the caller knows it came from a
        # different project. This ADDITIVE path only fires when the
        # resolved project itself is empty AND no explicit soul_id was
        # given, so existing single-project users see no change.
        fallback = find_most_recent_soul_across_projects(
            exclude_project_path=project_path
        )
        if fallback:
            recovered = fallback["soul"]
            out = {
                "status": "revived",
                "soul": asdict(recovered),
                "context": _format_revival(recovered),
                "recovered_from_venture": recovered.project_path
                or fallback.get("project_hash", ""),
                "recovered_project_hash": fallback.get("project_hash", ""),
                "note": (
                    f"No soul for {project_path}; recovered the most recent "
                    f"soul from {recovered.project_path or fallback.get('project_hash', '')}."
                ),
            }
            if reconciled:
                out["reconciled_orphan"] = reconciled
            return out
        out = {
            "status": "no_souls",
            "message": f"No session souls found for {project_path}. Nothing to revive.",
            "hint": "Use delimit_soul_capture to save session state before ending.",
        }
        if reconciled:
            out["reconciled_orphan"] = reconciled
        return out

    out = {
        "status": "revived",
        "soul": asdict(soul),
        "context": _format_revival(soul),
    }
    if reconciled:
        out["reconciled_orphan"] = reconciled
    return out


def should_auto_capture(
    context_fullness: float = 0.0,
    session_age_minutes: int = 0,
    last_capture_minutes_ago: int = -1,
) -> bool:
    """Determine if we should auto-capture a soul.

    Triggers:
    - Context > 70% full
    - Session > 30 minutes old with no capture in the last 15 minutes
    - Explicit session end (handled by caller, not this function)
    """
    if context_fullness >= 0.7:
        return True
    if session_age_minutes >= 30 and (last_capture_minutes_ago < 0 or last_capture_minutes_ago >= 15):
        return True
    return False
