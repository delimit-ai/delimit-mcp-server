"""
Self-repair watcher daemon — production wrapper around WatcherDaemon.

LED-191. Wraps `ai.self_repair.watcher.WatcherDaemon` in the same
start/stop/status lifecycle pattern used by `ai.social_daemon` and
`ai.inbox_daemon` so the watcher can run continuously under systemd
(or as a plain background thread) instead of only on-demand from the
CLI's `delimit self-repair check` path.

Responsibilities:
  - Background-thread lifecycle (start, stop, status) with idempotent
    start (a second `start` returns "already_running", does NOT spawn
    a duplicate thread).
  - Per-process state persistence at ~/.delimit/self_repair_daemon_state.json
    so an operator can inspect last_pass / breaches_emitted / consecutive
    _failures without an MCP call.
  - Honor `DELIMIT_SELF_REPAIR_PAUSE=1` — the watcher itself already
    short-circuits inside `run_one_pass`, but we re-check at the daemon
    layer so the status reads "paused (env)" instead of "running, 0
    breaches" (less confusing for operators).
  - Auto-stop after `MAX_CONSECUTIVE_FAILURES` (default 3) consecutive
    pass-level exceptions, mirroring inbox_daemon's circuit breaker. A
    transient breach-evaluation error inside a single function does NOT
    count — only top-level run_one_pass exceptions do.

Can run as:
  - Standalone process: `python -m ai.self_repair_daemon`
  - MCP tool: `delimit_self_repair_daemon(action="start"|"stop"|"status")`
  - Systemd unit: `deploy/systemd/delimit-self-repair.service`

NO auto-start on import — the operator must explicitly call `start()`
or invoke the systemd unit. This is intentional because the watcher
emits founder-facing emails on every breach and accidentally booting
it during a unit-test import would deliver spurious alerts.

Pre-flight escalation hard-stops live in the apply-layer (see
`ai/self_repair/apply.py`); the daemon is purely a pass-loop driver
and does NOT bypass them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ai.self_repair.watcher import (
    STATE_PATH as _WATCHER_STATE_PATH,
    WatcherDaemon,
    _DEFAULT_PASS_INTERVAL_SECONDS,
)

logger = logging.getLogger("delimit.ai.self_repair_daemon")

# ── Configuration ────────────────────────────────────────────────────

# Default pass interval is 1 hour, overridable via env. The interval is
# read once at module load + can be overridden per-start() call so tests
# can drive the loop at sub-second cadence.
DEFAULT_POLL_INTERVAL = int(
    os.environ.get(
        "DELIMIT_SELF_REPAIR_POLL_INTERVAL",
        str(_DEFAULT_PASS_INTERVAL_SECONDS),
    )
)
MAX_CONSECUTIVE_FAILURES = int(
    os.environ.get("DELIMIT_SELF_REPAIR_MAX_FAILURES", "3")
)

DAEMON_STATE_PATH = Path.home() / ".delimit" / "self_repair_daemon_state.json"
ALERTS_DIR = Path.home() / ".delimit" / "alerts"
ALERT_FILE = ALERTS_DIR / "self_repair_daemon.json"


# ── Daemon state ─────────────────────────────────────────────────────


class SelfRepairDaemonState:
    """Thread-safe state for the self-repair watcher daemon.

    Mirrors the SocialDaemonState shape so the MCP tool surface returns
    a homogeneous dict across daemons (operators / dashboards can switch
    between them without re-learning the keys).
    """

    def __init__(self) -> None:
        self.running: bool = False
        self.last_pass: Optional[str] = None
        self.last_pass_breaches: int = 0
        self.breaches_emitted: int = 0
        self.consecutive_failures: int = 0
        self.total_passes: int = 0
        self.stopped_reason: Optional[str] = None
        self.poll_interval_seconds: int = DEFAULT_POLL_INTERVAL
        self.started_at: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_pass": self.last_pass,
                "last_pass_breaches": self.last_pass_breaches,
                "breaches_emitted": self.breaches_emitted,
                "consecutive_failures": self.consecutive_failures,
                "total_passes": self.total_passes,
                "stopped_reason": self.stopped_reason,
                "poll_interval_seconds": self.poll_interval_seconds,
                "started_at": self.started_at,
                "watcher_state_path": str(_WATCHER_STATE_PATH),
                "paused_env": (
                    os.environ.get("DELIMIT_SELF_REPAIR_PAUSE", "").strip()
                    == "1"
                ),
            }

    def record_pass(self, breaches: int) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.last_pass = datetime.now(timezone.utc).isoformat()
            self.last_pass_breaches = breaches
            self.breaches_emitted += breaches
            self.total_passes += 1

    def record_failure(self) -> int:
        with self._lock:
            self.consecutive_failures += 1
            self.total_passes += 1
            self.last_pass = datetime.now(timezone.utc).isoformat()
            return self.consecutive_failures

    def reset_for_start(self, *, poll_interval: int) -> None:
        with self._lock:
            self.running = True
            self.stopped_reason = None
            self.consecutive_failures = 0
            self.poll_interval_seconds = poll_interval
            self.started_at = datetime.now(timezone.utc).isoformat()
            self._stop_event.clear()


_daemon_state = SelfRepairDaemonState()
# Module-level handle to the daemon used by the loop. Replaced on every
# `start_daemon` call so tests can pass a fixture-rebased WatcherDaemon
# without losing the global lifecycle bookkeeping.
_active_watcher: Optional[WatcherDaemon] = None


# ── State file persistence ────────────────────────────────────────────


def _persist_state() -> None:
    """Write the current daemon state to disk so an operator can poll
    it without an MCP call. Best-effort — write failures are logged but
    do not crash the daemon."""
    try:
        DAEMON_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DAEMON_STATE_PATH.with_suffix(DAEMON_STATE_PATH.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_daemon_state.to_dict(), f, indent=2, sort_keys=True)
        tmp.replace(DAEMON_STATE_PATH)
    except OSError as exc:
        logger.warning(
            "self_repair_daemon: cannot persist state to %s: %s",
            DAEMON_STATE_PATH,
            exc,
        )


def _load_persisted_state() -> Dict[str, Any]:
    """Read the on-disk daemon state. Returns {} if missing or unreadable.

    Used after a daemon restart to surface "what was the daemon doing
    before it died" without requiring the caller to poll a live thread.
    """
    if not DAEMON_STATE_PATH.exists():
        return {}
    try:
        with open(DAEMON_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "self_repair_daemon: cannot read persisted state %s: %s",
            DAEMON_STATE_PATH,
            exc,
        )
        return {}


# ── Pass loop ─────────────────────────────────────────────────────────


def _run_single_pass() -> Dict[str, Any]:
    """Run one watcher pass and update daemon state.

    Returns a small dict suitable for logging. Failure-isolated — any
    exception is caught, recorded, and counted toward the consecutive-
    failures circuit breaker.
    """
    global _active_watcher
    if _active_watcher is None:
        # Defensive: should never happen if start_daemon ran first.
        return {"error": "no active watcher"}

    # Re-check the env pause flag at the daemon layer so status() reads
    # cleanly when an operator flips DELIMIT_SELF_REPAIR_PAUSE=1 without
    # restarting the systemd unit. The watcher itself also honors it.
    if os.environ.get("DELIMIT_SELF_REPAIR_PAUSE", "").strip() == "1":
        logger.info(
            "self_repair_daemon: env pause flag set; pass skipped"
        )
        _daemon_state.record_pass(breaches=0)
        return {"paused": True, "breaches": 0}

    try:
        breaches = _active_watcher.run_one_pass()
        count = len(breaches) if breaches else 0
        _daemon_state.record_pass(breaches=count)
        logger.info(
            "self_repair_daemon: pass complete (%d breach notifications fired)",
            count,
        )
        return {"breaches": count}
    except Exception as exc:  # pragma: no cover - defensive
        failures = _daemon_state.record_failure()
        logger.exception(
            "self_repair_daemon: pass failed (%d/%d consecutive): %s",
            failures,
            MAX_CONSECUTIVE_FAILURES,
            exc,
        )
        return {"error": str(exc), "consecutive_failures": failures}


def _daemon_loop() -> None:
    """Main watcher loop. Exits cleanly on stop event or after
    MAX_CONSECUTIVE_FAILURES consecutive failures."""
    interval = _daemon_state.poll_interval_seconds
    logger.info(
        "self_repair_daemon started (poll interval %ds, max failures %d)",
        interval,
        MAX_CONSECUTIVE_FAILURES,
    )

    while not _daemon_state._stop_event.is_set():
        result = _run_single_pass()
        _persist_state()

        if "error" in result:
            failures = result.get("consecutive_failures", 0)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                reason = (
                    f"{MAX_CONSECUTIVE_FAILURES} consecutive self-repair pass "
                    f"failures. Last error: {result.get('error')}"
                )
                with _daemon_state._lock:
                    _daemon_state.stopped_reason = reason
                    _daemon_state.running = False
                logger.error(
                    "self_repair_daemon: circuit breaker tripped — %s",
                    reason,
                )
                _persist_state()
                return

        # Wait until the next pass or until stop is requested.
        _daemon_state._stop_event.wait(timeout=interval)

    with _daemon_state._lock:
        _daemon_state.running = False
        if _daemon_state.stopped_reason is None:
            _daemon_state.stopped_reason = "manual_stop"
    _persist_state()
    logger.info("self_repair_daemon stopped (reason=%s)",
                _daemon_state.stopped_reason)


# ── Public lifecycle ──────────────────────────────────────────────────


def start_daemon(
    *,
    poll_interval_seconds: Optional[int] = None,
    config_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    base_kpi_dir: Optional[Path] = None,
    sources_base_dir: Optional[Path] = None,
    watcher: Optional[WatcherDaemon] = None,
) -> Dict[str, Any]:
    """Start the self-repair watcher daemon in a background thread.

    Idempotent: a second call while the daemon is running returns
    `{"status": "already_running", ...}` without spawning a duplicate
    thread.

    Args:
        poll_interval_seconds: Override the default 1h cadence. Tests
            pass a small value (e.g. 0.05s) to drive multiple passes
            inside a unit test.
        config_path / state_path / notify_fn / base_kpi_dir / sources_base_dir:
            Forwarded to a freshly-constructed WatcherDaemon. Ignored if
            `watcher` is provided.
        watcher: A pre-built WatcherDaemon (used by tests). When set,
            the kwargs above are ignored.
    """
    global _active_watcher

    if _daemon_state.running:
        return {"status": "already_running", **_daemon_state.to_dict()}

    interval = (
        poll_interval_seconds
        if poll_interval_seconds is not None
        else DEFAULT_POLL_INTERVAL
    )

    if watcher is None:
        watcher = WatcherDaemon(
            config_path=config_path,
            state_path=state_path or _WATCHER_STATE_PATH,
            notify_fn=notify_fn,  # type: ignore[arg-type]
            pass_interval_seconds=int(interval) if interval >= 1 else 1,
            base_kpi_dir=base_kpi_dir,
            sources_base_dir=sources_base_dir,
        ) if notify_fn is not None else WatcherDaemon(
            config_path=config_path,
            state_path=state_path or _WATCHER_STATE_PATH,
            pass_interval_seconds=int(interval) if interval >= 1 else 1,
            base_kpi_dir=base_kpi_dir,
            sources_base_dir=sources_base_dir,
        )

    _active_watcher = watcher
    _daemon_state.reset_for_start(poll_interval=interval)
    _persist_state()

    thread = threading.Thread(
        target=_daemon_loop, name="self-repair-daemon", daemon=True
    )
    _daemon_state._thread = thread
    thread.start()

    return {"status": "started", **_daemon_state.to_dict()}


def stop_daemon(*, join_timeout: float = 5.0) -> Dict[str, Any]:
    """Stop the daemon. Idempotent — safe to call when not running."""
    if not _daemon_state.running:
        # Surface the persisted state so callers see the last reason
        # the daemon stopped (circuit breaker, manual, etc.).
        return {"status": "not_running", **_daemon_state.to_dict()}

    _daemon_state._stop_event.set()
    with _daemon_state._lock:
        if _daemon_state.stopped_reason is None:
            _daemon_state.stopped_reason = "manual_stop"

    thread = _daemon_state._thread
    if thread is not None:
        thread.join(timeout=join_timeout)

    with _daemon_state._lock:
        _daemon_state.running = False
    _persist_state()
    return {"status": "stopped", **_daemon_state.to_dict()}


def get_daemon_status() -> Dict[str, Any]:
    """Return current daemon status. Falls back to persisted state if
    the in-process daemon is not running (e.g. after a process restart)."""
    live = _daemon_state.to_dict()
    if not live["running"]:
        persisted = _load_persisted_state()
        if persisted:
            # Mark which fields came from disk so operators can tell.
            live["persisted_snapshot"] = persisted
    return live


def run_once(
    *,
    config_path: Optional[Path] = None,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    base_kpi_dir: Optional[Path] = None,
    sources_base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """One-shot: run a single watcher pass without spawning a thread.

    Useful for cron-driven setups where systemd timers handle cadence
    instead of the daemon's own sleep loop. Does NOT touch
    `_daemon_state.running` — it only updates the pass counters.
    """
    watcher_kwargs: Dict[str, Any] = {
        "config_path": config_path,
        "base_kpi_dir": base_kpi_dir,
        "sources_base_dir": sources_base_dir,
    }
    if notify_fn is not None:
        watcher_kwargs["notify_fn"] = notify_fn
    watcher = WatcherDaemon(**watcher_kwargs)

    global _active_watcher
    _active_watcher = watcher
    return _run_single_pass()


# ── Standalone runner ────────────────────────────────────────────────


def main() -> None:  # pragma: no cover - exercised only under systemd
    """Run as standalone process. Used by `python -m ai.self_repair_daemon`
    and the systemd unit."""
    import argparse
    import signal
    import sys

    parser = argparse.ArgumentParser(
        prog="ai.self_repair_daemon",
        description="Run the Delimit self-repair watcher continuously",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Pass interval in seconds (default {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (cron-driven setups)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if args.once:
        result = run_once()
        if "error" in result:
            logger.error("self_repair_daemon: pass failed: %s", result["error"])
            sys.exit(1)
        return

    # Long-running mode: install signal handlers for clean shutdown.
    def _handle_signal(signum, _frame):
        sig_name = signal.Signals(signum).name
        logger.info(
            "self_repair_daemon: received %s — initiating shutdown", sig_name
        )
        _daemon_state._stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = args.interval if args.interval is not None else DEFAULT_POLL_INTERVAL
    start_daemon(poll_interval_seconds=interval)
    # In standalone mode, block the main thread until the loop exits.
    thread = _daemon_state._thread
    if thread is not None:
        thread.join()


if __name__ == "__main__":  # pragma: no cover
    main()
