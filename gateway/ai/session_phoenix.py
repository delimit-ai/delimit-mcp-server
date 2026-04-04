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


def _prune_souls(proj_dir: Path) -> None:
    """Keep only the latest MAX_SOULS_PER_PROJECT souls per project."""
    soul_files = sorted(
        [f for f in proj_dir.iterdir() if f.name != "latest.json" and f.suffix == ".json"],
        key=lambda f: f.name,
    )
    while len(soul_files) > MAX_SOULS_PER_PROJECT:
        oldest = soul_files.pop(0)
        oldest.unlink(missing_ok=True)


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
    return soul


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


def _format_revival(soul: SessionSoul) -> str:
    """Format a soul into a readable context string for any AI model."""
    lines = []
    lines.append("=" * 60)
    lines.append("SESSION PHOENIX -- Revived Session Context")
    lines.append("=" * 60)
    lines.append("")

    lines.append(f"Soul ID:      {soul.soul_id}")
    lines.append(f"Captured:     {soul.created_at}")
    lines.append(f"Source Model: {soul.source_model}")
    lines.append(f"Project:      {soul.project_path}")
    lines.append("")

    # Current task
    lines.append("--- ACTIVE TASK ---")
    if soul.active_task:
        lines.append(f"  {soul.active_task}")
        lines.append(f"  Status: {soul.task_status}")
    else:
        lines.append("  (none recorded)")
    lines.append("")

    # Decisions
    if soul.decisions:
        lines.append("--- KEY DECISIONS ---")
        for d in soul.decisions:
            lines.append(f"  - {d}")
        lines.append("")

    # Files
    if soul.files_modified or soul.files_created:
        lines.append("--- FILES CHANGED ---")
        for f in soul.files_modified:
            lines.append(f"  M {f}")
        for f in soul.files_created:
            lines.append(f"  + {f}")
        lines.append("")

    # Context
    if soul.key_context:
        lines.append("--- KEY CONTEXT ---")
        for c in soul.key_context:
            lines.append(f"  - {c}")
        lines.append("")

    # Blockers
    if soul.blockers:
        lines.append("--- BLOCKERS ---")
        for b in soul.blockers:
            lines.append(f"  ! {b}")
        lines.append("")

    # Next steps
    if soul.next_steps:
        lines.append("--- NEXT STEPS ---")
        for i, s in enumerate(soul.next_steps, 1):
            lines.append(f"  {i}. {s}")
        lines.append("")

    # Git state
    lines.append("--- GIT STATE ---")
    lines.append(f"  Branch: {soul.git_branch or '(unknown)'}")
    lines.append(f"  SHA:    {soul.git_sha or '(unknown)'}")
    lines.append(f"  Uncommitted changes: {soul.uncommitted_changes}")
    lines.append("")

    # Token stats
    if soul.tokens_used or soul.context_fullness:
        lines.append("--- SESSION STATS ---")
        if soul.tokens_used:
            lines.append(f"  Tokens used: ~{soul.tokens_used:,}")
        if soul.context_fullness:
            lines.append(f"  Context fullness: {soul.context_fullness:.0%}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def revive(project_path: str = "", soul_id: str = "") -> Dict[str, Any]:
    """Revive the latest session soul for this project.

    Returns a structured dict with both the raw soul data and a
    formatted context string that can be injected into any model.
    """
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

    # Get latest
    soul = get_latest_soul(project_path)
    if not soul:
        return {
            "status": "no_souls",
            "message": f"No session souls found for {project_path}. Nothing to revive.",
            "hint": "Use delimit_soul_capture to save session state before ending.",
        }

    return {
        "status": "revived",
        "soul": asdict(soul),
        "context": _format_revival(soul),
    }


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
