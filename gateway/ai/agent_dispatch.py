"""Multi-agent orchestration — dispatch, track, and govern engineering tasks.

Agents are tracked via a local task store. Each dispatched task gets a unique
ID with status tracking. The dispatcher doesn't spawn actual AI processes
(that's the host AI's job) — it provides the governance layer:
assignment, tracking, policy enforcement, and handoff protocol.

Storage: ~/.delimit/agents/tasks.json
Audit trail: ~/.delimit/agents/audit.jsonl
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENTS_DIR = Path.home() / ".delimit" / "agents"
TASKS_FILE = AGENTS_DIR / "tasks.json"
AUDIT_FILE = AGENTS_DIR / "audit.jsonl"

VALID_PRIORITIES = {"P0", "P1", "P2"}
VALID_ASSIGNEES = {"claude", "codex", "gemini", "any"}
VALID_STATUSES = {"dispatched", "in_progress", "done", "handed_off", "failed"}


def _ensure_dir():
    """Create the agents directory if it doesn't exist."""
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_tasks() -> Dict[str, Any]:
    """Load all tasks from the tasks file."""
    if not TASKS_FILE.exists():
        return {}
    try:
        return json.loads(TASKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tasks(tasks: Dict[str, Any]):
    """Write all tasks back to the tasks file."""
    _ensure_dir()
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))


def _append_audit(entry: Dict[str, Any]):
    """Append an entry to the audit trail."""
    _ensure_dir()
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def dispatch_task(
    title: str,
    description: str = "",
    assignee: str = "any",
    priority: str = "P1",
    tools_needed: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    context: str = "",
) -> Dict[str, Any]:
    """Create a tracked agent task.

    Returns:
        Dict with task_id, task details, and a structured prompt for the host AI.
    """
    if not title or not title.strip():
        return {"error": "title is required"}

    assignee = assignee.lower().strip() if assignee else "any"
    if assignee not in VALID_ASSIGNEES:
        return {"error": f"assignee must be one of: {', '.join(sorted(VALID_ASSIGNEES))}"}

    priority = priority.upper().strip() if priority else "P1"
    if priority not in VALID_PRIORITIES:
        return {"error": f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}"}

    task_id = f"AGT-{uuid.uuid4().hex[:8].upper()}"

    task = {
        "id": task_id,
        "title": title.strip(),
        "description": description.strip(),
        "assignee": assignee,
        "priority": priority,
        "tools_needed": tools_needed or [],
        "constraints": constraints or [],
        "context": context.strip(),
        "status": "dispatched",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files_changed": [],
        "result": "",
        "handoffs": [],
    }

    tasks = _load_tasks()
    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "dispatch",
        "task_id": task_id,
        "title": title.strip(),
        "assignee": assignee,
        "priority": priority,
    })

    # Build a structured prompt that the host AI can pass to a subagent
    prompt = _build_agent_prompt(task)

    return {
        "status": "dispatched",
        "task_id": task_id,
        "task": task,
        "agent_prompt": prompt,
        "message": f"Task {task_id} dispatched to {assignee} ({priority})",
    }


def _build_agent_prompt(task: Dict[str, Any]) -> str:
    """Build a structured prompt for a subagent to execute the task."""
    lines = [
        f"## Agent Task: {task['id']}",
        f"**Title:** {task['title']}",
    ]
    if task.get("description"):
        lines.append(f"**Description:** {task['description']}")
    lines.append(f"**Priority:** {task['priority']}")
    lines.append(f"**Assignee:** {task['assignee']}")

    if task.get("context"):
        lines.append(f"\n**Context:**\n{task['context']}")

    if task.get("tools_needed"):
        lines.append(f"\n**Tools needed:** {', '.join(task['tools_needed'])}")

    if task.get("constraints"):
        lines.append(f"\n**Constraints:**")
        for c in task["constraints"]:
            lines.append(f"- {c}")

    lines.append(f"\n**When done:** Call `delimit_agent_complete` with task_id='{task['id']}' and your result.")

    return "\n".join(lines)


def get_agent_status(task_id: str = "") -> Dict[str, Any]:
    """Get the status of a specific task, or list all active tasks."""
    tasks = _load_tasks()

    if not task_id or not task_id.strip():
        return list_active_agents()

    task_id = task_id.strip().upper()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    return {
        "status": "ok",
        "task": tasks[task_id],
    }


def list_active_agents() -> Dict[str, Any]:
    """Return all tasks that are not done or failed."""
    tasks = _load_tasks()
    active = {
        tid: t for tid, t in tasks.items()
        if t.get("status") in ("dispatched", "in_progress", "handed_off")
    }
    completed = {
        tid: t for tid, t in tasks.items()
        if t.get("status") in ("done", "failed")
    }

    return {
        "status": "ok",
        "active_count": len(active),
        "completed_count": len(completed),
        "active_tasks": list(active.values()),
        "summary": [
            {"id": t["id"], "title": t["title"], "status": t["status"],
             "assignee": t["assignee"], "priority": t["priority"]}
            for t in active.values()
        ],
    }


def complete_task(
    task_id: str,
    result: str = "",
    files_changed: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Mark a dispatched task as done."""
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()

    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    if task["status"] == "done":
        return {"error": f"Task {task_id} is already marked done"}

    task["status"] = "done"
    task["result"] = result.strip()
    task["files_changed"] = files_changed or []
    task["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "complete",
        "task_id": task_id,
        "result": result.strip()[:200],
        "files_changed": files_changed or [],
    })

    return {
        "status": "completed",
        "task_id": task_id,
        "task": task,
        "message": f"Task {task_id} marked as done",
    }


def handoff_task(
    task_id: str,
    to_model: str,
    context: str = "",
) -> Dict[str, Any]:
    """Transfer a task from one AI model to another."""
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not to_model or not to_model.strip():
        return {"error": "to_model is required"}

    task_id = task_id.strip().upper()
    to_model = to_model.lower().strip()

    if to_model not in VALID_ASSIGNEES - {"any"}:
        return {"error": f"to_model must be one of: {', '.join(sorted(VALID_ASSIGNEES - {'any'}))}"}

    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    if task["status"] == "done":
        return {"error": f"Task {task_id} is already done, cannot hand off"}

    from_model = task["assignee"]
    task["handoffs"].append({
        "from": from_model,
        "to": to_model,
        "context": context.strip(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    task["assignee"] = to_model
    task["status"] = "handed_off"
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Rebuild the prompt with handoff context
    if context.strip():
        task["context"] = (task.get("context", "") + "\n\n---\n**Handoff context from " +
                          from_model + ":**\n" + context.strip()).strip()

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "handoff",
        "task_id": task_id,
        "from": from_model,
        "to": to_model,
        "context_snippet": context.strip()[:200],
    })

    prompt = _build_agent_prompt(task)

    return {
        "status": "handed_off",
        "task_id": task_id,
        "from_model": from_model,
        "to_model": to_model,
        "task": task,
        "agent_prompt": prompt,
        "message": f"Task {task_id} handed off from {from_model} to {to_model}",
    }


def enforce_constraints(task_id: str, action: str) -> Dict[str, Any]:
    """Check if an action is allowed given the task's constraints.

    Returns:
        Dict with 'allowed' (bool) and 'reason' if denied.
    """
    if not task_id or not task_id.strip():
        return {"allowed": True, "reason": "No task_id provided, no constraints to check"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()

    if task_id not in tasks:
        return {"allowed": True, "reason": f"Task {task_id} not found, defaulting to allow"}

    task = tasks[task_id]
    constraints = task.get("constraints", [])

    if not constraints:
        return {"allowed": True, "reason": "No constraints on this task"}

    action_lower = action.lower().strip() if action else ""

    # Check each constraint against the action
    for constraint in constraints:
        c = constraint.lower().strip()

        if c == "read-only":
            write_keywords = ["write", "edit", "create", "delete", "modify", "deploy", "push", "commit"]
            if any(kw in action_lower for kw in write_keywords):
                return {
                    "allowed": False,
                    "reason": f"Constraint 'read-only' blocks action: {action}",
                    "constraint": constraint,
                }

        elif c == "no-deploy" or c == "no-deploys":
            deploy_keywords = ["deploy", "publish", "release", "rollback"]
            if any(kw in action_lower for kw in deploy_keywords):
                return {
                    "allowed": False,
                    "reason": f"Constraint '{constraint}' blocks action: {action}",
                    "constraint": constraint,
                }

        elif c == "must-lint" or c == "must_lint":
            # This is an affirmative constraint — doesn't block, just flags
            pass

        elif c.startswith("no-"):
            # Generic "no-X" constraint
            blocked = c[3:]
            if blocked in action_lower:
                return {
                    "allowed": False,
                    "reason": f"Constraint '{constraint}' blocks action: {action}",
                    "constraint": constraint,
                }

    return {"allowed": True, "reason": "All constraints passed"}


def link_ledger_item(task_id: str, ledger_item_id: str) -> Dict[str, Any]:
    """Link a dispatched agent task to a ledger item (LED-xxx or STR-xxx).

    This creates a bidirectional relationship so the dashboard can show
    which agent is working on which ledger item.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not ledger_item_id or not ledger_item_id.strip():
        return {"error": "ledger_item_id is required"}

    task_id = task_id.strip().upper()
    ledger_item_id = ledger_item_id.strip().upper()

    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    linked = task.get("linked_ledger_items", [])
    if ledger_item_id not in linked:
        linked.append(ledger_item_id)
    task["linked_ledger_items"] = linked
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "link_ledger",
        "task_id": task_id,
        "ledger_item_id": ledger_item_id,
    })

    return {
        "status": "linked",
        "task_id": task_id,
        "ledger_item_id": ledger_item_id,
        "all_linked": linked,
        "message": f"Task {task_id} linked to {ledger_item_id}",
    }


def get_agent_dashboard() -> Dict[str, Any]:
    """Return a full dashboard view of all agent activity.

    Groups tasks by status and assignee, includes audit trail summary,
    and handoff history.
    """
    tasks = _load_tasks()

    by_assignee: Dict[str, List[Dict]] = {}
    by_status: Dict[str, int] = {}
    handoff_count = 0

    for t in tasks.values():
        assignee = t.get("assignee", "unknown")
        status = t.get("status", "unknown")
        by_assignee.setdefault(assignee, []).append(t)
        by_status[status] = by_status.get(status, 0) + 1
        handoff_count += len(t.get("handoffs", []))

    # Recent audit entries
    recent_audit: List[Dict] = []
    if AUDIT_FILE.exists():
        try:
            lines = AUDIT_FILE.read_text().strip().split("\n")
            for line in lines[-20:]:
                try:
                    recent_audit.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    return {
        "status": "ok",
        "total_tasks": len(tasks),
        "by_status": by_status,
        "by_assignee": {
            model: {
                "total": len(model_tasks),
                "active": sum(1 for t in model_tasks if t["status"] in ("dispatched", "in_progress", "handed_off")),
                "done": sum(1 for t in model_tasks if t["status"] == "done"),
                "tasks": [
                    {"id": t["id"], "title": t["title"], "status": t["status"],
                     "priority": t.get("priority", "P1"),
                     "linked_ledger": t.get("linked_ledger_items", [])}
                    for t in model_tasks
                ],
            }
            for model, model_tasks in by_assignee.items()
        },
        "handoff_count": handoff_count,
        "recent_audit": recent_audit,
    }
