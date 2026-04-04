"""
Social sensing daemon for Delimit.

Runs social discovery scans (X, Reddit, GitHub, Dev.to) on a regular interval.
Deduplicates findings and emits HTML draft emails for human approval.
Also monitors for direct replies to owned posts (LED-300).

Consensus 123: Part of the continuous sensing loop.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.social_daemon")

# ── Configuration ────────────────────────────────────────────────────
# Default to 15 minutes (900 seconds)
SCAN_INTERVAL = int(os.environ.get("DELIMIT_SOCIAL_SCAN_INTERVAL", "900"))
MAX_CONSECUTIVE_FAILURES = 3

ALERTS_DIR = Path.home() / ".delimit" / "alerts"
ALERT_FILE = ALERTS_DIR / "social_daemon.json"
DAEMON_STATE = Path.home() / ".delimit" / "social_daemon_state.json"
OWNER_ACTION_SUMMARY = Path.home() / ".delimit" / "owner_action_summary.json"

class SocialDaemonState:
    """Thread-safe state for the social sensing daemon."""

    def __init__(self):
        self.running = False
        self.last_scan: Optional[str] = None
        self.targets_found: int = 0
        self.consecutive_failures: int = 0
        self.total_scans: int = 0
        self.stopped_reason: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_scan": self.last_scan,
                "targets_found": self.targets_found,
                "consecutive_failures": self.consecutive_failures,
                "total_scans": self.total_scans,
                "stopped_reason": self.stopped_reason,
                "scan_interval_seconds": SCAN_INTERVAL,
            }

    def record_success(self, found: int):
        with self._lock:
            self.consecutive_failures = 0
            self.targets_found += found
            self.total_scans += 1
            self.last_scan = datetime.now(timezone.utc).isoformat()

    def record_failure(self) -> int:
        with self._lock:
            self.consecutive_failures += 1
            self.total_scans += 1
            self.last_scan = datetime.now(timezone.utc).isoformat()
            return self.consecutive_failures

_daemon_state = SocialDaemonState()

def scan_once() -> Dict[str, Any]:
    """Execute a single social scan cycle and process results (LED-238)."""
    from ai.social_target import scan_targets, process_targets
    
    try:
        # 1. DISCOVER: Scan all platforms
        targets = scan_targets(platforms=["x", "reddit", "github", "hn", "devto"])
        found = len(targets)
        
        # 2. ORCHESTRATE: Process discovered targets (LED-238)
        # draft_replies=True -> emits social_draft emails
        # create_ledger=True -> creates strategic ledger items
        processed = process_targets(targets, draft_replies=True, create_ledger=True)
        OWNER_ACTION_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        OWNER_ACTION_SUMMARY.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "targets_found": found,
            "owner_actions": len(processed.get("owner_actions", [])),
            "drafted": len(processed.get("drafted", [])),
            "ledger_items": len(processed.get("ledger_items", [])),
            "strategy_items": len(processed.get("strategy_items", [])),
        }, indent=2) + "\n")
        
        _daemon_state.record_success(found)
        return {
            "targets_found": found,
            "processed": processed
        }
    except Exception as e:
        failures = _daemon_state.record_failure()
        logger.error("Social scan failed: %s", e)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            reason = f"3 consecutive social scan failures. Last: {e}"
            _daemon_state.stopped_reason = reason
            _daemon_state.running = False
            _daemon_state._stop_event.set()
        return {"error": str(e), "consecutive_failures": failures}

def _daemon_loop() -> None:
    """Main scanning loop."""
    logger.info("Social daemon started. Scanning every %d seconds.", SCAN_INTERVAL)

    while not _daemon_state._stop_event.is_set():
        try:
            result = scan_once()
            if "error" in result:
                logger.warning("Scan cycle error: %s", result["error"])
            else:
                logger.info("Scan complete: %d new targets", result.get("targets_found", 0))
        except Exception as e:
            logger.error("Unexpected error in social daemon loop: %s", e)
            failures = _daemon_state.record_failure()
            if failures >= MAX_CONSECUTIVE_FAILURES:
                break

        _daemon_state._stop_event.wait(timeout=SCAN_INTERVAL)

    _daemon_state.running = False
    logger.info("Social daemon stopped.")

def start_daemon() -> Dict[str, Any]:
    """Start the social daemon in a background thread."""
    if _daemon_state.running:
        return {"status": "already_running", **_daemon_state.to_dict()}

    _daemon_state.running = True
    _daemon_state.stopped_reason = None
    _daemon_state.consecutive_failures = 0
    _daemon_state._stop_event.clear()

    thread = threading.Thread(target=_daemon_loop, name="social-daemon", daemon=True)
    _daemon_state._thread = thread
    thread.start()

    return {"status": "started", **_daemon_state.to_dict()}

def stop_daemon() -> Dict[str, Any]:
    """Stop the social daemon."""
    if not _daemon_state.running:
        return {"status": "not_running", **_daemon_state.to_dict()}

    _daemon_state._stop_event.set()
    _daemon_state.stopped_reason = "manual_stop"
    if _daemon_state._thread:
        _daemon_state._thread.join(timeout=5)
    _daemon_state.running = False

    return {"status": "stopped", **_daemon_state.to_dict()}

def get_daemon_status() -> Dict[str, Any]:
    """Get current daemon status."""
    return _daemon_state.to_dict()

def main():
    """Run as standalone process."""
    import argparse
    parser = argparse.ArgumentParser(description="Delimit social sensing daemon")
    parser.add_argument("--interval", type=int, help="Scan interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if args.interval:
        global SCAN_INTERVAL
        SCAN_INTERVAL = args.interval

    if args.once:
        scan_once()
        return

    _daemon_state.running = True
    _daemon_loop()

if __name__ == "__main__":
    main()
