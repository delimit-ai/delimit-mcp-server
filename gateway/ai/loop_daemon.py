"""
Background scheduler for the governed build/social/deploy loops.
Wraps run_governed_iteration / run_social_iteration in a daemon thread
that fires on a configurable interval. Preserves the pull-based triage
pattern — each tick logs the returned task_id, and the orchestrating
Claude session reads the log to handle triage.
"""

import os
import json
import time
import threading
import atexit
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_DIR = Path(os.path.expanduser("~/.delimit/state"))
LOG_DIR = Path(os.path.expanduser("~/.delimit/logs"))

# Process-local registry of running daemons: session_id -> DaemonContext
_daemons: dict = {}
_registry_lock = threading.Lock()


class DaemonContext:
    def __init__(self, session_id: str, interval_seconds: int, loop_type: str):
        self.session_id = session_id
        self.interval_seconds = interval_seconds
        self.loop_type = loop_type
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.started_at: Optional[float] = None
        self.last_tick_at: Optional[float] = None
        self.ticks_run = 0
        self.tasks_pulled = 0
        self.last_task_id: Optional[str] = None
        self.daemon_status = "stopped"  # running | paused | stopped | tripped
        self.trip_reason: Optional[str] = None
        self.lock = threading.Lock()

    def state_path(self) -> Path:
        return STATE_DIR / f"loop_daemon_{self.session_id}.json"

    def log_path(self) -> Path:
        return LOG_DIR / f"loop_daemon_{self.session_id}.jsonl"

    def persist(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            payload = {
                "session_id": self.session_id,
                "interval_seconds": self.interval_seconds,
                "loop_type": self.loop_type,
                "started_at": self.started_at,
                "last_tick_at": self.last_tick_at,
                "ticks_run": self.ticks_run,
                "tasks_pulled": self.tasks_pulled,
                "last_task_id": self.last_task_id,
                "daemon_status": self.daemon_status,
                "trip_reason": self.trip_reason,
            }
        tmp = self.state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.state_path())

    def log_tick(self, payload: dict) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        with self.log_path().open("a") as f:
            f.write(json.dumps(payload) + "\n")


def _tick_executor(ctx: "DaemonContext") -> None:
    """Run the bounded worker-pool v2 executor once per daemon tick.

    Fire-and-forget: the executor has its own pause file and error handling,
    so any failure here is logged but never crashes the tick.
    """
    try:
        from ai.workers.executor import poll_and_execute, is_paused
    except Exception as exc:
        ctx.log_tick({"event": "executor_import_error", "error": str(exc)})
        return
    if is_paused():
        ctx.log_tick({"event": "executor_paused"})
        return
    try:
        summary = poll_and_execute(live=True, executed_by=f"daemon:{ctx.session_id}")
        ctx.log_tick({
            "event": "executor_tick",
            "candidates": summary.get("candidates", 0),
            "attempted": summary.get("attempted", 0),
            "results": summary.get("results", []),
        })
    except Exception as exc:
        ctx.log_tick({"event": "executor_tick_error", "error": str(exc)})


def _run_forever(ctx: DaemonContext) -> None:
    """Daemon thread body. Fires the loop iteration every interval_seconds."""
    from ai.loop_engine import run_governed_iteration, run_social_iteration, run_full_cycle, loop_status

    ctx.started_at = time.time()
    ctx.daemon_status = "running"
    ctx.persist()

    while not ctx.stop_event.is_set():
        # Check safeguards via loop_status (reads cost/iterations from the session)
        try:
            status = loop_status(ctx.session_id) or {}
            if status.get("status") in ("paused", "stopped", "circuit_broken", "cost_cap_reached"):
                with ctx.lock:
                    ctx.daemon_status = "tripped"
                    ctx.trip_reason = status.get("status")
                ctx.log_tick({"event": "trip", "reason": ctx.trip_reason, "status": status})
                ctx.persist()
                break
        except Exception as e:
            ctx.log_tick({"event": "status_check_error", "error": str(e)})

        # Fire one iteration
        try:
            if ctx.loop_type == "cycle":
                # LED-917 + LED-966: daemon ran sense-only historically because
                # there was no worker pool. LED-981 shipped the bounded
                # worker-pool v2 executor, so the cycle loop now also ticks
                # the executor — it only touches approved work orders, uses a
                # typed whitelist, and respects two kill switches
                # (pause_dispatch + pause_executor).
                result = run_full_cycle(ctx.session_id, cycle_mode="sense")
                _tick_executor(ctx)
            elif ctx.loop_type == "social":
                result = run_social_iteration(ctx.session_id)
            else:
                result = run_governed_iteration(ctx.session_id)
                _tick_executor(ctx)
            task_id = None
            if isinstance(result, dict):
                task_id = result.get("task_id")
                if not task_id and isinstance(result.get("result"), dict):
                    task_id = result["result"].get("task_id")
            with ctx.lock:
                ctx.ticks_run += 1
                ctx.last_tick_at = time.time()
                if task_id:
                    ctx.last_task_id = task_id
                    ctx.tasks_pulled += 1
            ctx.log_tick({
                "event": "tick",
                "tick": ctx.ticks_run,
                "result_status": (result or {}).get("status") if isinstance(result, dict) else None,
                "task_id": task_id,
                "interval_seconds": ctx.interval_seconds,
            })
            ctx.persist()
        except Exception as e:
            ctx.log_tick({"event": "tick_error", "error": str(e), "error_type": type(e).__name__})
            ctx.persist()

        # Sleep in small increments so stop_event can fire responsively
        slept = 0.0
        while slept < ctx.interval_seconds and not ctx.stop_event.is_set():
            chunk = min(1.0, ctx.interval_seconds - slept)
            time.sleep(chunk)
            slept += chunk

    with ctx.lock:
        if ctx.daemon_status != "tripped":
            ctx.daemon_status = "stopped"
    ctx.log_tick({"event": "daemon_stopped", "daemon_status": ctx.daemon_status})
    ctx.persist()


def start(session_id: str, interval_seconds: int = 900, loop_type: str = "build") -> dict:
    """Start a background daemon for the given session."""
    with _registry_lock:
        existing = _daemons.get(session_id)
        if existing and existing.thread and existing.thread.is_alive():
            return {
                "status": "already_running",
                "session_id": session_id,
                "daemon_status": existing.daemon_status,
                "interval_seconds": existing.interval_seconds,
                "started_at": existing.started_at,
            }
        ctx = DaemonContext(session_id, interval_seconds, loop_type)
        t = threading.Thread(
            target=_run_forever,
            args=(ctx,),
            daemon=True,
            name=f"loop-daemon-{session_id}",
        )
        ctx.thread = t
        _daemons[session_id] = ctx
        t.start()
    return {
        "status": "started",
        "session_id": session_id,
        "interval_seconds": interval_seconds,
        "loop_type": loop_type,
        "state_path": str(ctx.state_path()),
        "log_path": str(ctx.log_path()),
    }


def stop(session_id: str) -> dict:
    """Signal the daemon to stop. Returns after the thread exits (or timeout 10s)."""
    with _registry_lock:
        ctx = _daemons.get(session_id)
    if not ctx:
        return {"status": "not_found", "session_id": session_id}
    ctx.stop_event.set()
    if ctx.thread:
        ctx.thread.join(timeout=10)
    return {
        "status": "stopped",
        "session_id": session_id,
        "ticks_run": ctx.ticks_run,
        "tasks_pulled": ctx.tasks_pulled,
    }


def status(session_id: str) -> dict:
    """Return live daemon state. Reads from registry + state file fallback."""
    with _registry_lock:
        ctx = _daemons.get(session_id)
    if ctx:
        alive = bool(ctx.thread and ctx.thread.is_alive())
        with ctx.lock:
            return {
                "session_id": session_id,
                "daemon_status": ctx.daemon_status,
                "alive": alive,
                "interval_seconds": ctx.interval_seconds,
                "loop_type": ctx.loop_type,
                "started_at": ctx.started_at,
                "last_tick_at": ctx.last_tick_at,
                "ticks_run": ctx.ticks_run,
                "tasks_pulled": ctx.tasks_pulled,
                "last_task_id": ctx.last_task_id,
                "trip_reason": ctx.trip_reason,
                "next_tick_at": (ctx.last_tick_at + ctx.interval_seconds) if ctx.last_tick_at else None,
                "log_path": str(ctx.log_path()),
            }
    # Fallback: read state file
    state_file = STATE_DIR / f"loop_daemon_{session_id}.json"
    if state_file.exists():
        data = json.loads(state_file.read_text())
        data["alive"] = False
        data["source"] = "state_file"
        return data
    return {"status": "not_found", "session_id": session_id}


def _shutdown_all():
    """atexit handler — signal all daemons to stop."""
    with _registry_lock:
        for ctx in _daemons.values():
            ctx.stop_event.set()


atexit.register(_shutdown_all)
