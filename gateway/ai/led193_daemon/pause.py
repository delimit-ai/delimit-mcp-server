"""LED-193 pause-state management.

The daemon is paused when ``~/.delimit/led193_paused.json`` exists.
Founder must manually clear (e.g. ``rm ~/.delimit/led193_paused.json``)
to resume — the daemon will NEVER auto-clear its own pause state.

Triggered by:
    - 3 consecutive failures (audit-log driven)
    - explicit founder pause

The kill switch (env var ``DELIMIT_LED193_DAEMON_DISABLED=1``) is
checked separately at script start in ``scripts/led193_cron.py`` —
even a non-paused daemon is blocked when the kill switch is set.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.ai.led193_daemon.pause")

PAUSE_FILE = Path.home() / ".delimit" / "led193_paused.json"


def is_paused(*, pause_file: Optional[Path] = None) -> bool:
    """True iff the pause file exists."""
    target = pause_file or PAUSE_FILE
    return target.exists()


def pause(
    *,
    reason: str,
    pause_file: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create the pause file. Idempotent — overwrites if already present.

    Returns the payload written.
    """
    target = pause_file or PAUSE_FILE
    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    if extra:
        # Don't let extra clobber required keys.
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as exc:  # pragma: no cover
        logger.warning("led193_daemon: failed to write pause file: %s", exc)
    return payload


def clear(*, pause_file: Optional[Path] = None) -> bool:
    """Remove the pause file. Returns True if a file was removed.

    Founder-only intent — the daemon should never call this on itself.
    Tests may call it for cleanup.
    """
    target = pause_file or PAUSE_FILE
    if target.exists():
        try:
            target.unlink()
            return True
        except OSError as exc:  # pragma: no cover
            logger.warning("led193_daemon: failed to clear pause file: %s", exc)
    return False


def kill_switch_active() -> bool:
    """Check the env-var kill switch. True blocks all execution."""
    val = (os.environ.get("DELIMIT_LED193_DAEMON_DISABLED") or "").strip().lower()
    return val in ("1", "true", "yes", "on")
