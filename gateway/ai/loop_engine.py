"""Autonomous build loop engine — governed, throttled, cross-model.

Provides the core loop primitives that any AI model can use via MCP:
- next_task: get the next prioritized item with safeguard checks
- task_complete: record completion, check if loop should continue
- loop_status: current session metrics
- loop_config: configure safeguards

Session state persisted at ~/.delimit/loop/sessions/<session_id>.json
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

LOOP_DIR = Path.home() / ".delimit" / "loop"
SESSIONS_DIR = LOOP_DIR / "sessions"

# Actions the AI model must never auto-execute without human approval
DEFAULT_REQUIRE_APPROVAL = ["deploy", "social_post", "outreach", "publish"]

VALID_STATUSES = {"running", "paused", "stopped", "circuit_broken"}


def _ensure_dir():
    """Create the loop sessions directory if it doesn't exist."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(session_id: str) -> Path:
    """Return the filesystem path for a session state file."""
    return SESSIONS_DIR / f"{session_id}.json"


def _load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session state from disk. Returns None if not found."""
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_session(session: Dict[str, Any]):
    """Persist session state to disk."""
    _ensure_dir()
    path = _session_path(session["session_id"])
    path.write_text(json.dumps(session, indent=2))


def _create_session(session_id: str = "") -> Dict[str, Any]:
    """Create a new loop session with default safeguards."""
    if not session_id:
        session_id = str(uuid.uuid4())[:12]
    session = {
        "session_id": session_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iterations": 0,
        "max_iterations": 50,
        "cost_incurred": 0.0,
        "cost_cap": 5.0,
        "errors": 0,
        "error_threshold": 3,
        "tasks_completed": [],
        "auto_consensus": False,
        "require_approval_for": list(DEFAULT_REQUIRE_APPROVAL),
        "status": "running",
    }
    _save_session(session)
    return session


def _get_or_create_session(session_id: str = "") -> Dict[str, Any]:
    """Load an existing session or create a new one."""
    if session_id:
        existing = _load_session(session_id)
        if existing:
            return existing
    return _create_session(session_id)


def _check_safeguards(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Check all safeguards. Returns a STOP action if any are tripped, else None."""
    if session.get("status") == "paused":
        return {
            "action": "STOP",
            "reason": "Loop is paused. Call loop_config to resume.",
            "safeguard": "paused",
        }

    if session.get("status") == "stopped":
        return {
            "action": "STOP",
            "reason": "Loop has been stopped.",
            "safeguard": "stopped",
        }

    if session.get("status") == "circuit_broken":
        return {
            "action": "STOP",
            "reason": f"Circuit breaker tripped after {session['errors']} errors.",
            "safeguard": "circuit_breaker",
        }

    if session["iterations"] >= session["max_iterations"]:
        return {
            "action": "STOP",
            "reason": f"Reached max iterations ({session['max_iterations']}).",
            "safeguard": "max_iterations",
        }

    if session["cost_incurred"] >= session["cost_cap"]:
        return {
            "action": "STOP",
            "reason": f"Cost cap reached (${session['cost_incurred']:.2f} >= ${session['cost_cap']:.2f}).",
            "safeguard": "cost_cap",
        }

    if session["errors"] >= session["error_threshold"]:
        session["status"] = "circuit_broken"
        _save_session(session)
        return {
            "action": "STOP",
            "reason": f"Circuit breaker: {session['errors']} errors hit threshold ({session['error_threshold']}).",
            "safeguard": "circuit_breaker",
        }

    return None


def _get_open_items(venture: str = "", project_path: str = ".") -> List[Dict[str, Any]]:
    """Query the ledger for open items, sorted by priority."""
    from ai.ledger_manager import list_items
    result = list_items(status="open", project_path=project_path)
    items = []
    for ledger_items in result.get("items", {}).values():
        items.extend(ledger_items)

    # Sort by priority
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda x: priority_order.get(x.get("priority", "P2"), 9))
    return items


def _risk_level(risk: str) -> int:
    """Convert risk string to numeric level for comparison."""
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(risk.lower(), 2)


def _filter_actionable(items: List[Dict[str, Any]], max_risk: str = "") -> List[Dict[str, Any]]:
    """Filter out owner-only items and apply risk filtering.

    Owner-only items are those with source='owner' or tags containing 'owner-action'.
    """
    filtered = []
    for item in items:
        # Skip owner-only items
        tags = item.get("tags", [])
        if "owner-action" in tags or "owner-only" in tags:
            continue
        if item.get("source") == "owner":
            continue

        # Risk filtering
        if max_risk:
            item_risk = item.get("risk", "")
            if item_risk and _risk_level(item_risk) > _risk_level(max_risk):
                continue

        filtered.append(item)
    return filtered


def _resolve_project_path(venture: str) -> str:
    """Resolve a venture name or path to a project directory path."""
    if not venture:
        return "."
    # Direct path — use as-is
    if venture.startswith("/") or venture.startswith("~"):
        return str(Path(venture).expanduser())
    if venture.startswith(".") or os.sep in venture:
        return str(Path(venture).resolve())
    # Try registered ventures
    try:
        from ai.ledger_manager import list_ventures
        ventures = list_ventures()
        for name, info in ventures.get("ventures", {}).items():
            if name == venture or venture in name:
                return info.get("path", ".")
    except Exception:
        pass
    return "."


def _session_summary(session: Dict[str, Any]) -> Dict[str, Any]:
    """Return a concise session summary for inclusion in responses."""
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "iterations": session["iterations"],
        "max_iterations": session["max_iterations"],
        "cost_incurred": round(session["cost_incurred"], 4),
        "cost_cap": session["cost_cap"],
        "errors": session["errors"],
        "error_threshold": session["error_threshold"],
        "tasks_done": len(session.get("tasks_completed", [])),
        "auto_consensus": session.get("auto_consensus", False),
    }


def next_task(
    venture: str = "",
    max_risk: str = "",
    session_id: str = "",
) -> Dict[str, Any]:
    """Get the next task to work on with safeguard checks.

    Returns:
        Dict with action: BUILD (with task), CONSENSUS (generate new items), or STOP.
    """
    session = _get_or_create_session(session_id)

    # Check safeguards
    stop = _check_safeguards(session)
    if stop:
        stop["session"] = _session_summary(session)
        return stop

    # Resolve venture path
    project_path = _resolve_project_path(venture)

    # Get open items
    items = _get_open_items(venture=venture, project_path=project_path)
    actionable = _filter_actionable(items, max_risk=max_risk)

    if not actionable:
        if session.get("auto_consensus"):
            return {
                "action": "CONSENSUS",
                "message": "No actionable items. Run consensus to generate new work.",
                "session": _session_summary(session),
            }
        return {
            "action": "STOP",
            "reason": "No actionable items in the ledger.",
            "safeguard": "empty_ledger",
            "session": _session_summary(session),
        }

    task = actionable[0]

    # Check if this task requires approval
    require_approval = session.get("require_approval_for", [])
    task_tags = task.get("tags", [])
    needs_approval = any(tag in require_approval for tag in task_tags)
    task_type = task.get("type", "")
    if task_type in require_approval:
        needs_approval = True

    result = {
        "action": "BUILD",
        "task": task,
        "remaining_items": len(actionable) - 1,
        "session": _session_summary(session),
    }
    if needs_approval:
        result["approval_required"] = True
        result["approval_reason"] = "Task type or tags match require_approval_for list."

    return result


def task_complete(
    task_id: str,
    result: str = "",
    cost_incurred: float = 0.0,
    error: str = "",
    session_id: str = "",
    venture: str = "",
) -> Dict[str, Any]:
    """Mark current task done and get the next one.

    Records completion, updates session metrics, returns the next task.
    """
    session = _get_or_create_session(session_id)

    # Update metrics
    session["iterations"] += 1
    session["cost_incurred"] += cost_incurred

    if error:
        session["errors"] += 1
        session["tasks_completed"].append({
            "task_id": task_id,
            "status": "error",
            "error": error,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cost": cost_incurred,
        })
    else:
        session["tasks_completed"].append({
            "task_id": task_id,
            "status": "done",
            "result": result[:500] if result else "",
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cost": cost_incurred,
        })

    _save_session(session)

    # Mark ledger item as done (best-effort)
    if not error:
        try:
            from ai.ledger_manager import update_item
            project_path = _resolve_project_path(venture)
            update_item(item_id=task_id, status="done", note=result[:200] if result else "Completed via build loop", project_path=project_path)
        except Exception:
            pass  # Never let ledger sync break the loop

    # Return the next task
    return next_task(venture=venture, session_id=session["session_id"])


def _list_sessions() -> List[Dict[str, Any]]:
    """List all sessions, most recent first."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
            sessions.append(s)
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    return sessions


def loop_status(session_id: str = "") -> Dict[str, Any]:
    """Return current session metrics."""
    if not session_id:
        # Try to find the most recent session
        sessions = _list_sessions()
        if not sessions:
            return {"error": "No active loop sessions found."}
        session_id = sessions[0]["session_id"]

    session = _load_session(session_id)
    if not session:
        return {"error": f"Session {session_id} not found."}

    return {
        "session": _session_summary(session),
        "tasks_completed": session.get("tasks_completed", []),
        "safeguards": {
            "max_iterations": session["max_iterations"],
            "cost_cap": session["cost_cap"],
            "error_threshold": session["error_threshold"],
            "require_approval_for": session.get("require_approval_for", []),
        },
    }


def loop_config(
    session_id: str = "",
    max_iterations: int = 0,
    cost_cap: float = 0.0,
    auto_consensus: Optional[bool] = None,
    error_threshold: int = 0,
    status: str = "",
    require_approval_for: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Update session configuration. Only provided values are changed."""
    session = _get_or_create_session(session_id)

    changes = {}
    if max_iterations > 0:
        session["max_iterations"] = max_iterations
        changes["max_iterations"] = max_iterations
    if cost_cap > 0:
        session["cost_cap"] = cost_cap
        changes["cost_cap"] = cost_cap
    if auto_consensus is not None:
        session["auto_consensus"] = auto_consensus
        changes["auto_consensus"] = auto_consensus
    if error_threshold > 0:
        session["error_threshold"] = error_threshold
        changes["error_threshold"] = error_threshold
    if status and status in VALID_STATUSES:
        session["status"] = status
        changes["status"] = status
    if require_approval_for is not None:
        session["require_approval_for"] = require_approval_for
        changes["require_approval_for"] = require_approval_for

    _save_session(session)

    return {
        "session_id": session["session_id"],
        "changes": changes,
        "current_config": _session_summary(session),
    }
