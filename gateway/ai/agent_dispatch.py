"""Multi-agent orchestration — dispatch, track, and govern engineering tasks.

Agents are tracked via a local task store. Each dispatched task gets a unique
ID with status tracking. The dispatcher doesn't spawn actual AI processes
(that's the host AI's job) — it provides the governance layer:
assignment, tracking, policy enforcement, and handoff protocol.

Storage: ~/.delimit/agents/tasks.json
Audit trail: ~/.delimit/agents/audit.jsonl
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# LED-3759: reuse the shared ephemeral-path guard (single source; do not fork).
try:
    from ai.registry_guards import is_ephemeral_path
except ImportError:  # pragma: no cover - flat import layout
    from registry_guards import is_ephemeral_path

AGENTS_DIR = Path.home() / ".delimit" / "agents"
TASKS_FILE = AGENTS_DIR / "tasks.json"
AUDIT_FILE = AGENTS_DIR / "audit.jsonl"
PAUSE_FILE = Path.home() / ".delimit" / "pause_dispatch"

# LED-3759: the DEFAULT (production) store location, captured at import. The
# ephemeral guard only ever redirects writes away from THIS default store — if
# a caller/test has explicitly relocated ``AGENTS_DIR`` (e.g. to an isolated
# tmp dir), that override is honored verbatim and the guard stays out of the
# way. This is what keeps every existing test that patches AGENTS_DIR working.
_DEFAULT_AGENTS_DIR = AGENTS_DIR

# LED-3759: when the dispatch context (cwd or the supplied venture path) is an
# ephemeral test/scratch path, task + audit writes redirect here instead of the
# real ~/.delimit/agents store, so tests never leak a live record into
# tasks.json. Kept under the delimit home so it is session-scoped (removed with
# the tmp HOME under the test harness) rather than an ever-growing /tmp dir.
# Real contexts never touch it.
EPHEMERAL_AGENTS_DIR = Path.home() / ".delimit" / "_ephemeral_scratch" / "agents"


def _effective_agents_dir(venture: str = "") -> Path:
    """Return the agents store dir, redirected to a throwaway dir in tests.

    Fires on the *path* (LED-3733/LED-3759 design), never on "am I in pytest":
    a real cwd/venture is never redirected, only /tmp + pytest/tempfile scratch.
    An explicit store override (``AGENTS_DIR`` reassigned away from the import
    default) is always honored — the guard only protects the default store.
    """
    if AGENTS_DIR != _DEFAULT_AGENTS_DIR:
        return AGENTS_DIR
    if is_ephemeral_path(os.getcwd()) or (venture and is_ephemeral_path(venture)):
        return EPHEMERAL_AGENTS_DIR
    return AGENTS_DIR

VALID_PRIORITIES = {"P0", "P1", "P2"}
VALID_ASSIGNEES = {"claude", "codex", "gemini", "any"}
VALID_STATUSES = {"dispatched", "in_progress", "done", "handed_off", "failed"}

# LED-876: auto-pause when dead-letter queue depth (stuck 'dispatched' tasks)
# hits this threshold. Prevents runaway dispatch when no workers are pulling.
DLQ_AUTO_PAUSE_THRESHOLD = 20

# LED-3514: task types whose lifecycle parks them at status=='dispatched' as a
# long-lived TARGET POOL rather than a worker-consumption queue. These are NOT
# a dead-letter backlog — they are read in place (e.g. outreach_substantive is
# the target pool that body-gen + the contributions dashboard consume), so they
# must be excluded from the DLQ depth or they spuriously trip the circuit
# breaker and globally pause ALL dispatch. The breaker still protects genuine
# worker-consumed task types; pool growth is bounded by each pool's own caps
# (the outreach daemon enforces a per-day dispatch cap + repo cooldown).
_DLQ_EXEMPT_TASK_TYPES = frozenset({"outreach_substantive"})

# LED-878: router table — resolves assignee='any' to a specific model at
# dispatch time based on task_type. This eliminates the dead-letter 'any'
# bucket without requiring a worker process to exist yet. The mapping is
# deliberately conservative: if the task type is unknown, fall through to
# gemini (cheapest, highest throughput) rather than pile onto claude.
TASK_TYPE_ROUTER = {
    # Outreach and social work — Gemini Flash is fast and cheap
    "outreach": "gemini",
    # LED-2214b: substantive github outreach gets the same default
    # routing as generic outreach (cheap, fast drafter) but is named
    # distinctly so a regression that resurrects the generic dispatch
    # path does not silently land here.
    "outreach_substantive": "gemini",
    "social": "gemini",
    "content": "gemini",
    "sensor": "gemini",
    # Engineering — Claude / Codex for code, Claude for governance
    "fix": "claude",
    "feat": "claude",
    "refactor": "claude",
    "test": "codex",
    "research": "gemini",
    "strategy": "gemini",
    "deliberation": "claude",
}
ROUTER_DEFAULT_ASSIGNEE = "gemini"

# STR-2202: concrete (non-"any") assignees the rank resolver may pick.
_CONCRETE_ASSIGNEES = VALID_ASSIGNEES - {"any"}

# STR-2202: minimum recorded outcomes for a model before prompt_drift.rank is
# trusted to resolve assignee="any". Below this the data is too thin, so we
# FALL BACK to the static TASK_TYPE_ROUTER (today's behavior). This is the
# fail-safe threshold — rank never *blocks* a dispatch, it only refines it.
_RANK_MIN_SAMPLES = 5

# STR-2202: variables keys that carry an explicit checkout/worktree path a
# dispatch will write into. Only when one is present do we auto-claim a
# checkout lock (fold 3) — without an explicit target we cannot safely
# identify the checkout, so we skip the claim rather than over-block.
_CHECKOUT_HINT_KEYS = ("checkout", "worktree", "checkout_path", "repo_path")

# Task types / result markers used to derive prompt_drift.success at completion.
_FAILURE_MARKERS = ("failed", "error:", "blocked", "could not", "unable to", "aborted")


def _resolve_any_assignee(task_type: str) -> str:
    """Resolve assignee='any' to a concrete model.

    STR-2202: prompt_drift.rank is the PRIMARY resolver — it picks the
    best-performing model for this task_type from RECORDED outcomes
    (Optimal-Tooling by mechanism). When data is thin or anything errors,
    fall back to the static TASK_TYPE_ROUTER (fail-safe — never blocks
    dispatch). Returns a concrete assignee name.
    """
    tt = (task_type or "").lower().strip()
    ranked = _rank_best_assignee(tt)
    if ranked:
        return ranked
    return TASK_TYPE_ROUTER.get(tt, ROUTER_DEFAULT_ASSIGNEE)


def _rank_best_assignee(task_type: str) -> Optional[str]:
    """Best concrete model per prompt_drift rankings, or None if data is thin.

    Best-effort: any failure returns None so the caller falls back to the
    static router. A model must have >= _RANK_MIN_SAMPLES recorded outcomes
    for its ranking to count.
    """
    try:
        from ai.prompt_drift import get_model_rankings

        ranked = get_model_rankings(task_type=task_type)
        if not isinstance(ranked, dict) or ranked.get("status") != "ok":
            return None
        # rankings are pre-sorted best-first (success rate desc, duration asc).
        for row in ranked.get("rankings", []):
            model = (row.get("model") or "").lower().strip()
            if model in _CONCRETE_ASSIGNEES and int(row.get("total_tasks", 0)) >= _RANK_MIN_SAMPLES:
                return model
        return None
    except Exception:  # pragma: no cover — resolver must never break dispatch
        return None


def _checkout_target(
    variables: Optional[Dict[str, Any]],
    constraints: Optional[List[str]],
) -> Optional[str]:
    """Return the checkout/worktree path this dispatch should claim, or None.

    Only claims when an explicit checkout hint is present in ``variables`` and
    the task is not read-only. No hint → None → no claim (safe, no over-block).
    """
    cons = {(c or "").lower().strip() for c in (constraints or [])}
    if "read-only" in cons or "readonly" in cons:
        return None
    for key in _CHECKOUT_HINT_KEYS:
        val = (variables or {}).get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _iso_epoch(ts: str) -> Optional[float]:
    """Parse a '%Y-%m-%dT%H:%M:%SZ' UTC timestamp to epoch seconds, or None."""
    if not ts:
        return None
    try:
        import calendar
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def _record_completion_drift(task: Dict[str, Any]) -> None:
    """Fold prompt_drift.record into the completion path (STR-2202 fold 1).

    Best-effort + FAIL-SAFE: any error here must NEVER change the completion
    result. Derives {model, task_type, success, duration} from data already on
    the task record — no separate manual call.
    """
    try:
        from ai.prompt_drift import record_result

        model = (task.get("assignee") or "").strip()
        if not model or model == "any":
            return
        result = (task.get("result") or "").strip()
        title = (task.get("title") or "").strip()
        # Stable-ish prompt identity for hashing/grouping across models.
        prompt = title or (task.get("context") or "").strip() or task.get("id", "")
        if not prompt:
            return

        task_type = (task.get("task_type") or "").strip() or "engineering"
        # success = task closed with a result and no explicit failure marker.
        low = result.lower()
        success = bool(result) and not any(m in low for m in _FAILURE_MARKERS)

        start = _iso_epoch(task.get("created_at", ""))
        end = _iso_epoch(task.get("completed_at", "")) or _iso_epoch(task.get("updated_at", ""))
        duration_ms = 0
        if start is not None and end is not None and end >= start:
            duration_ms = int((end - start) * 1000)

        record_result(
            prompt=prompt,
            model=model,
            result_summary=result or "(no result summary)",
            success=success,
            task_type=task_type,
            duration_ms=duration_ms,
        )
    except Exception:  # pragma: no cover — instrumentation must never break completion
        pass


def _release_checkout_claim(task: Dict[str, Any]) -> None:
    """Release any checkout lock this task holds (STR-2202 fold 3, best-effort)."""
    try:
        from ai.collision_detect import release_checkout

        release_checkout(task_id=task.get("id", ""))
    except Exception:  # pragma: no cover — release must never break completion/handoff
        pass


def _ensure_dir(base_dir: Optional[Path] = None):
    """Create the agents directory if it doesn't exist."""
    (base_dir or _effective_agents_dir()).mkdir(parents=True, exist_ok=True)


def _load_tasks(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load all tasks from the tasks file."""
    tasks_file = (base_dir or _effective_agents_dir()) / "tasks.json"
    if not tasks_file.exists():
        return {}
    try:
        return json.loads(tasks_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tasks(tasks: Dict[str, Any], base_dir: Optional[Path] = None):
    """Write all tasks back to the tasks file."""
    base = base_dir or _effective_agents_dir()
    _ensure_dir(base)
    (base / "tasks.json").write_text(json.dumps(tasks, indent=2))


def _append_audit(entry: Dict[str, Any], base_dir: Optional[Path] = None):
    """Append an entry to the audit trail."""
    base = base_dir or _effective_agents_dir()
    _ensure_dir(base)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(base / "audit.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def dispatch_task(
    title: str,
    description: str = "",
    assignee: str = "any",
    priority: str = "P1",
    tools_needed: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    context: str = "",
    task_type: str = "",
    venture: str = "",
    variables: Optional[Dict[str, Any]] = None,
    external_key: str = "",
    focus_bypass_reason: str = "",
) -> Dict[str, Any]:
    """Create a tracked agent task.

    LED-1829 (T3 focus gate): ``focus_bypass_reason`` is an optional one-line
    reason recorded when dispatching NON-Delimit engineering work while an
    advanceable Delimit P0/P1 remains open. This is ADVISORY only — it never
    blocks the dispatch; it attaches a ``focus_advisory`` block to the result
    and logs the bypass for the 30-day review. See ai/focus_gate.py.

    Returns:
        Dict with task_id, task details, and a structured prompt for the host AI.
    """
    if not title or not title.strip():
        return {"error": "title is required"}

    # LED-3759: resolve the effective store once, honoring an ephemeral venture
    # path in addition to an ephemeral cwd. Threaded into every task/audit write
    # below so a dispatch from a test context never touches the real store.
    agents_dir = _effective_agents_dir(venture)

    # LED-876: reject ghost "[VENTURE] Engage:  on x" titles with empty author
    # slot. The social_target fix drops these at the scanner, but keep this as
    # a belt-and-suspenders check since agent_dispatch has other callers too.
    stripped = title.strip()
    if "Engage:  on " in stripped or "Engage: on " in stripped:
        return {"error": f"rejected ghost engage task with empty author: {stripped!r}"}

    # LED-876: manual kill switch. Touch ~/.delimit/pause_dispatch to halt all
    # dispatches instantly without touching loop_config. Remove the file to
    # resume. Kept deliberately simple so it works from any shell.
    if PAUSE_FILE.exists():
        _append_audit({
            "action": "dispatch_rejected_paused",
            "title": stripped,
            "reason": str(PAUSE_FILE),
        }, agents_dir)
        return {"error": f"dispatch paused: {PAUSE_FILE} exists"}

    # LED-876: automatic circuit breaker. If the DLQ (count of 'dispatched'
    # tasks that never moved to in_progress/done/failed) exceeds the threshold,
    # auto-create the pause file and reject. This stops the cycle from growing
    # the queue unboundedly when workers aren't consuming.
    # LED-3514: exclude pool-lifecycle task types (e.g. outreach_substantive)
    # from the depth — they live at 'dispatched' by design and are not a stuck
    # backlog, so counting them spuriously pauses all dispatch.
    existing_tasks = _load_tasks(agents_dir)
    dlq_depth = sum(
        1 for t in existing_tasks.values()
        if t.get("status") == "dispatched"
        and t.get("task_type") not in _DLQ_EXEMPT_TASK_TYPES
    )
    if dlq_depth >= DLQ_AUTO_PAUSE_THRESHOLD:
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(
            f"auto-paused at {time.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"(dlq_depth={dlq_depth} >= {DLQ_AUTO_PAUSE_THRESHOLD})\n"
        )
        _append_audit({
            "action": "dispatch_auto_paused",
            "dlq_depth": dlq_depth,
            "threshold": DLQ_AUTO_PAUSE_THRESHOLD,
        }, agents_dir)
        return {
            "error": (
                f"auto-paused: DLQ depth {dlq_depth} >= {DLQ_AUTO_PAUSE_THRESHOLD}. "
                f"Clear stuck tasks then delete {PAUSE_FILE} to resume."
            )
        }

    assignee = assignee.lower().strip() if assignee else "any"
    if assignee not in VALID_ASSIGNEES:
        return {"error": f"assignee must be one of: {', '.join(sorted(VALID_ASSIGNEES))}"}

    # LED-878: resolve 'any' to a specific model via the router table so
    # tasks never land in a bucket no worker pulls from. The mapping uses
    # task_type as the primary key; if unknown, falls through to the
    # default (gemini — cheapest + highest throughput).
    if assignee == "any":
        # STR-2202: prompt_drift.rank is the primary resolver behind "any";
        # it falls back to the static TASK_TYPE_ROUTER when data is thin or on
        # any error (fail-safe — resolution never blocks the dispatch).
        routed = _resolve_any_assignee(task_type)
        if routed in VALID_ASSIGNEES and routed != "any":
            assignee = routed

    priority = priority.upper().strip() if priority else "P1"
    if priority not in VALID_PRIORITIES:
        return {"error": f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}"}

    # LED-1279: anti-duplicate gate. If the title/description/context tags an
    # LED that's already been shipped (i.e. there's a commit on main mentioning
    # the LED with date >= LED.created_at), refuse the dispatch and auto-close
    # the LED. Yesterday's AGT-65A61AD5 wasted three subagent cycles on
    # LED-1208/9/10, all of which had been shipped in commit 014fb5c on
    # 2026-05-03. This gate prevents that class of duplicate.
    try:
        from ai.dispatch_gate import evaluate_dispatch, extract_led_id, lookup_led_created_at

        led_id_for_gate = extract_led_id(title, description, context)
        if led_id_for_gate:
            led_created_at = lookup_led_created_at(led_id_for_gate)
            refusal = evaluate_dispatch(
                title=title,
                description=description,
                context=context,
                led_created_at=led_created_at,
            )
            if refusal is not None:
                _append_audit({
                    "action": "dispatch_refused_shipped",
                    "title": stripped,
                    "led_id": refusal.get("led_id"),
                    "shipped_in": refusal.get("shipped_in", {}).get("short_sha"),
                    "shipped_repo": refusal.get("shipped_in", {}).get("repo"),
                }, agents_dir)
                return refusal
    except Exception as e:  # pragma: no cover — gate must never crash dispatch
        # If the gate itself blows up, log it and proceed — losing a dispatch
        # to a gate bug is a worse failure mode than the duplicate it would
        # have caught.
        _append_audit({
            "action": "dispatch_gate_error",
            "error": str(e)[:200],
        }, agents_dir)

    # LED-1829 (T3 primary-venture focus gate): ADVISORY + LOGGED, never blocks.
    # When dispatching non-Delimit engineering work while an advanceable Delimit
    # P0/P1 remains open, emit an advisory and record a (standing or supplied)
    # bypass reason. Proceeding is always allowed — this is a nudge, not a gate.
    focus_advisory: Optional[Dict[str, Any]] = None
    try:
        from ai.focus_gate import evaluate_focus

        focus_advisory = evaluate_focus(
            venture=venture,
            task=title,
            task_type=task_type,
            description=description,
            context=context,
            bypass_reason=focus_bypass_reason,
        )
        if focus_advisory is not None:
            _append_audit({
                "action": "focus_gate_advisory",
                "title": stripped,
                "venture": venture,
                "warn": focus_advisory.get("warn"),
                "standing": focus_advisory.get("standing"),
            }, agents_dir)
    except Exception as e:  # pragma: no cover — advisory must never crash dispatch
        _append_audit({
            "action": "focus_gate_error",
            "error": str(e)[:200],
        }, agents_dir)

    tasks = _load_tasks(agents_dir)

    normalized_external_key = external_key.strip()
    if normalized_external_key:
        for existing in tasks.values():
            if existing.get("external_key") != normalized_external_key:
                continue
            if existing.get("status") in ("dispatched", "in_progress", "handed_off", "done"):
                prompt = _build_agent_prompt(existing)
                deduped_resp = {
                    "status": "deduped",
                    "task_id": existing["id"],
                    "task": existing,
                    "agent_prompt": prompt,
                    "message": f"Task {existing['id']} already exists for {normalized_external_key}",
                }
                if focus_advisory is not None:
                    deduped_resp["focus_advisory"] = focus_advisory
                return deduped_resp

    task_id = f"AGT-{uuid.uuid4().hex[:8].upper()}"

    # STR-2202 (fold 3): auto-claim the target checkout at dispatch start when
    # the dispatch carries an explicit checkout/worktree hint and is not
    # read-only. A CONFLICT is a HARD STOP — we refuse the dispatch so two
    # tasks never write the same checkout state. A plumbing ERROR (not a
    # conflict) is fail-safe: log and proceed with the dispatch unclaimed.
    checkout_lock_key = ""
    checkout_target = _checkout_target(variables, constraints)
    if checkout_target:
        try:
            from ai.collision_detect import claim_checkout

            claim = claim_checkout(checkout_target, model=assignee, task_id=task_id)
            if isinstance(claim, dict) and claim.get("status") == "collision":
                _append_audit({
                    "action": "dispatch_blocked_checkout_collision",
                    "title": stripped,
                    "checkout": checkout_target,
                    "held_by_task": claim.get("held_by_task", ""),
                }, agents_dir)
                return {
                    "error": (
                        f"checkout collision: {checkout_target} is held by "
                        f"{claim.get('held_by_task') or claim.get('held_by')} "
                        f"since {claim.get('held_since', 'unknown')}. "
                        f"Use an isolated worktree or wait."
                    ),
                    "status": "blocked_collision",
                    "collision": claim,
                }
            if isinstance(claim, dict) and claim.get("status") == "claimed":
                checkout_lock_key = claim.get("lock_key", "")
        except Exception as e:  # pragma: no cover — claim plumbing is fail-safe
            _append_audit({
                "action": "dispatch_checkout_claim_error",
                "title": stripped,
                "error": str(e)[:200],
            }, agents_dir)

    task = {
        "id": task_id,
        "title": title.strip(),
        "description": description.strip(),
        "assignee": assignee,
        "priority": priority,
        "tools_needed": tools_needed or [],
        "constraints": constraints or [],
        "context": context.strip(),
        "task_type": task_type.strip(),
        "venture": venture.strip(),
        "variables": variables or {},
        "external_key": normalized_external_key,
        "status": "dispatched",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files_changed": [],
        "result": "",
        "handoffs": [],
        "checkout_lock": checkout_lock_key,
    }

    tasks[task_id] = task
    _save_tasks(tasks, agents_dir)

    _append_audit({
        "action": "dispatch",
        "task_id": task_id,
        "title": title.strip(),
        "assignee": assignee,
        "priority": priority,
    }, agents_dir)

    # Build a structured prompt that the host AI can pass to a subagent
    prompt = _build_agent_prompt(task)

    dispatch_resp = {
        "status": "dispatched",
        "task_id": task_id,
        "task": task,
        "agent_prompt": prompt,
        "message": f"Task {task_id} dispatched to {assignee} ({priority})",
    }
    if focus_advisory is not None:
        dispatch_resp["focus_advisory"] = focus_advisory
    return dispatch_resp


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

    if task.get("variables"):
        lines.append("\n**Variables:**")
        for key, value in task["variables"].items():
            lines.append(f"- {key}: {value}")

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

    # STR-2202: fold prompt_drift.record + checkout-lock release into the
    # completion path. Both are best-effort and FAIL-SAFE — the completion
    # result above is already committed; instrumentation errors never change it.
    _record_completion_drift(task)
    _release_checkout_claim(task)

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

    # STR-2202 (fold 3): release the checkout claim on handoff — ownership is
    # moving; the new owner re-claims when it re-dispatches into a checkout.
    # Best-effort, fail-safe. The claimed key is cleared so state stays honest.
    _release_checkout_claim(task)
    task["checkout_lock"] = ""
    tasks[task_id] = task
    _save_tasks(tasks)

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


# STR-2202 / doc 25 §8 item #5: statuses eligible for stale auto-close. Only
# "dispatched" (an intent nothing ever picked up) closes by default — an
# in_progress/handed_off task may have an active worker, so it is left alone
# unless the caller explicitly widens the set.
_AUTO_CLOSE_DEFAULT_STATUSES = frozenset({"dispatched"})

# Default staleness horizon for the groom auto-close (hours). Overridable via
# DELIMIT_DISPATCH_STALE_HOURS. 0 disables auto-close entirely (fail-safe).
_DISPATCH_STALE_HOURS_DEFAULT = 48


def _dispatch_stale_hours_default() -> int:
    raw = os.environ.get("DELIMIT_DISPATCH_STALE_HOURS", "").strip()
    if not raw:
        return _DISPATCH_STALE_HOURS_DEFAULT
    try:
        val = int(raw)
        return val if val >= 0 else _DISPATCH_STALE_HOURS_DEFAULT
    except ValueError:
        return _DISPATCH_STALE_HOURS_DEFAULT


def auto_close_stale_dispatches(
    threshold_hours: Optional[int] = None,
    dry_run: bool = True,
    statuses: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Auto-close dispatch intents that have gone stale (doc 25 §8 item #5).

    Wired into the existing groom pass so the swamp of never-consumed dispatch
    intents (~26 >24h at design time) drains automatically instead of piling
    up in the Board Report's exceptions queue.

    Discipline (mirrors ``ledger_manager.auto_cancel_stale``):
      * ``dry_run=True`` (default) returns a plan and writes nothing; the
        caller passes ``dry_run=False`` explicitly to apply.
      * Pool-lifecycle task types (``_DLQ_EXEMPT_TASK_TYPES`` — e.g.
        ``outreach_substantive``) are NEVER closed: they live at
        ``dispatched`` as a target pool by design, not as a stuck backlog.
      * Only ``statuses`` (default ``{"dispatched"}``) are eligible; an
        in_progress/handed_off task may have an active worker.
      * ``threshold_hours == 0`` disables the sweep (fail-safe no-op).
      * Applied closes set ``status="failed"`` with an ``auto_closed`` marker
        + ``close_reason`` and an audit entry, so the action is fully
        traceable and reversible (re-dispatch restores the work).

    Returns a summary dict; never raises into the groom cron.
    """
    if threshold_hours is None:
        threshold_hours = _dispatch_stale_hours_default()
    statuses = statuses or _AUTO_CLOSE_DEFAULT_STATUSES

    if threshold_hours == 0:
        return {
            "dry_run": dry_run,
            "threshold_hours": 0,
            "would_close" if dry_run else "closed": [],
            "summary": {"scanned": 0, "stale": 0, "closed": 0,
                        "note": "DELIMIT_DISPATCH_STALE_HOURS=0 disables auto-close"},
        }

    base = _effective_agents_dir()
    tasks = _load_tasks(base)
    now = time.time()
    cutoff = now - threshold_hours * 3600

    stale: List[Dict[str, Any]] = []
    for tid, t in tasks.items():
        if t.get("status") not in statuses:
            continue
        if t.get("task_type") in _DLQ_EXEMPT_TASK_TYPES:
            continue
        ref_ts = t.get("updated_at") or t.get("created_at") or ""
        epoch = _iso_epoch(ref_ts)
        if epoch is None or epoch >= cutoff:
            continue
        stale.append(t)

    records = [
        {"id": t.get("id"), "title": (t.get("title") or "")[:80],
         "status": t.get("status"), "task_type": t.get("task_type", ""),
         "last_seen": t.get("updated_at") or t.get("created_at")}
        for t in stale
    ]

    key = "would_close" if dry_run else "closed"
    if dry_run or not stale:
        return {
            "dry_run": dry_run,
            "threshold_hours": threshold_hours,
            key: records,
            "summary": {"scanned": len(tasks), "stale": len(stale),
                        "closed": 0, "truncated": False},
        }

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = f"auto-closed: stale dispatch intent > {threshold_hours}h"
    closed = 0
    for t in stale:
        t["status"] = "failed"
        t["auto_closed"] = True
        t["close_reason"] = reason
        t["closed_at"] = stamp
        t["updated_at"] = stamp
        tasks[t["id"]] = t
        closed += 1
    _save_tasks(tasks, base)
    _append_audit({
        "action": "auto_close_stale",
        "closed": [r["id"] for r in records],
        "threshold_hours": threshold_hours,
    }, base)

    return {
        "dry_run": False,
        "threshold_hours": threshold_hours,
        "closed": records,
        "summary": {"scanned": len(tasks), "stale": len(stale),
                    "closed": closed, "truncated": False},
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
                     "linked_ledger": t.get("linked_ledger_items", []),
                     "task_type": t.get("task_type", ""),
                     "venture": t.get("venture", ""),
                     "variables": t.get("variables", {})}
                    for t in model_tasks
                ],
            }
            for model, model_tasks in by_assignee.items()
        },
        "handoff_count": handoff_count,
        "recent_audit": recent_audit,
    }
