#!/usr/bin/env python3
"""
Standalone runner for the Delimit inbox polling daemon.

Designed for use with systemd or manual invocation. Adds:
- Structured logging with timestamps
- Graceful SIGTERM handling for clean systemd stop
- PID file to prevent duplicate instances
- Startup validation of required configuration

Usage:
    # Via systemd (see deploy/inbox-daemon.service)
    systemctl start delimit-inbox-daemon

    # Manual foreground run
    python3 ai/inbox_daemon_runner.py

    # Single poll cycle (for testing)
    python3 ai/inbox_daemon_runner.py --once

Environment variables:
    DELIMIT_SMTP_PASS              Required. IMAP/SMTP password.
    DELIMIT_INBOX_POLL_INTERVAL    Poll interval in seconds (default: 300).
    DELIMIT_HOME                   Delimit config directory (default: ~/.delimit).
    PYTHONPATH                     Must include the gateway root for ai.* imports.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure the gateway root is on sys.path so ai.* imports work
_gateway_root = Path(__file__).resolve().parent.parent
if str(_gateway_root) not in sys.path:
    sys.path.insert(0, str(_gateway_root))

# PID file to prevent duplicate instances
PID_DIR = Path(os.environ.get("DELIMIT_HOME", Path.home() / ".delimit"))
PID_FILE = PID_DIR / "inbox-daemon.pid"


def _setup_logging() -> logging.Logger:
    """Configure structured logging for journald and console."""
    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        stream=sys.stdout,
    )
    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("imaplib").setLevel(logging.WARNING)
    return logging.getLogger("delimit.inbox_daemon_runner")


def _write_pid() -> None:
    """Write PID file. Check for stale processes first."""
    PID_DIR.mkdir(parents=True, exist_ok=True)

    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if the old process is still running
            os.kill(old_pid, 0)
            # Process exists -- abort to prevent duplicates
            print(
                f"ERROR: Another inbox daemon is running (PID {old_pid}). "
                f"Remove {PID_FILE} if stale.",
                file=sys.stderr,
            )
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale PID file -- safe to overwrite
            pass
        except OSError:
            pass

    PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    """Remove PID file on clean shutdown."""
    try:
        if PID_FILE.exists():
            current_pid = PID_FILE.read_text().strip()
            if current_pid == str(os.getpid()):
                PID_FILE.unlink()
    except OSError:
        pass


def _validate_config(logger: logging.Logger) -> bool:
    """Validate required configuration before starting the daemon."""
    ok = True

    if not os.environ.get("DELIMIT_SMTP_PASS"):
        # Check if the notify module can load credentials from config
        try:
            from ai.notify import _load_smtp_account, IMAP_USER
            if IMAP_USER:
                account = _load_smtp_account(IMAP_USER)
                if account and (account.get("pass") or account.get("password")):
                    logger.info("SMTP credentials loaded from config for %s", IMAP_USER)
                else:
                    logger.error(
                        "DELIMIT_SMTP_PASS not set and no credentials found in config for %s",
                        IMAP_USER,
                    )
                    ok = False
            else:
                logger.error("DELIMIT_SMTP_PASS not set and IMAP_USER not configured")
                ok = False
        except ImportError:
            logger.error("DELIMIT_SMTP_PASS not set and ai.notify module not importable")
            ok = False
    else:
        logger.info("SMTP credentials provided via environment")

    return ok


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Delimit inbox daemon runner -- persistent email governance polling",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override poll interval in seconds",
    )
    args = parser.parse_args()

    logger = _setup_logging()
    logger.info(
        "Delimit inbox daemon runner starting (PID %d, Python %s)",
        os.getpid(),
        sys.version.split()[0],
    )

    # Validate config before doing anything else
    if not _validate_config(logger):
        logger.error("Configuration validation failed. Exiting.")
        sys.exit(1)

    # Import the daemon module (after PYTHONPATH is set up)
    from ai.inbox_daemon import (
        _daemon_state,
        _daemon_loop,
        poll_once,
        POLL_INTERVAL,
    )

    # Override poll interval if requested
    if args.interval is not None:
        import ai.inbox_daemon
        ai.inbox_daemon.POLL_INTERVAL = args.interval
        logger.info("Poll interval overridden to %d seconds", args.interval)

    # Single-shot mode
    if args.once:
        logger.info("Running single poll cycle (--once mode)")
        result = poll_once()
        if "error" in result:
            logger.error("Poll failed: %s", result["error"])
            sys.exit(1)
        logger.info(
            "Poll complete: %d processed, %d forwarded",
            result.get("processed", 0),
            result.get("forwarded", 0),
        )
        return

    # Write PID file (only for long-running mode)
    _write_pid()

    # Graceful shutdown handler
    def _handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received %s -- initiating graceful shutdown", sig_name)
        _daemon_state._stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Start the daemon loop (blocks until stop event)
    logger.info(
        "Inbox daemon entering main loop (poll interval: %ds)",
        ai.inbox_daemon.POLL_INTERVAL,
    )
    _daemon_state.running = True
    _daemon_state._stop_event.clear()

    try:
        _daemon_loop()
    except Exception as e:
        logger.critical("Daemon loop crashed: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        _daemon_state.running = False
        _remove_pid()
        logger.info("Inbox daemon runner exiting cleanly")


if __name__ == "__main__":
    main()
