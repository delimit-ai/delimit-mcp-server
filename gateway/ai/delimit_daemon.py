"""
Unified Delimit Daemon (LED-193).

Consolidates three long-running daemons into a single process:
  - inbox_daemon (5m cadence)
  - social_daemon (15m cadence)
  - self_repair_daemon (1h cadence)

Retains the individual modules' internal state files and thread-level
encapsulation to minimize blast radius and ensure existing MCP interfaces
(status checks) continue to work without modification.
"""

import time
import logging
import signal
import sys

from ai.inbox_daemon import start_daemon as start_inbox, stop_daemon as stop_inbox
from ai.social_daemon import start_daemon as start_social, stop_daemon as stop_social
from ai.self_repair_daemon import start_daemon as start_self_repair, stop_daemon as stop_self_repair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("delimit.daemon_runner")

def _handle_sigterm(signum, frame):
    logger.info("Received SIGTERM, shutting down all daemons...")
    try:
        stop_inbox()
    except Exception as e:
        logger.error(f"Error stopping inbox: {e}")
    try:
        stop_social()
    except Exception as e:
        logger.error(f"Error stopping social: {e}")
    try:
        stop_self_repair()
    except Exception as e:
        logger.error(f"Error stopping self_repair: {e}")
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    logger.info("Starting unified delimit_daemon (LED-193)...")
    
    inbox_res = start_inbox()
    logger.info(f"Inbox daemon: {inbox_res.get('status')}")
    
    social_res = start_social()
    logger.info(f"Social daemon: {social_res.get('status')}")
    
    repair_res = start_self_repair()
    logger.info(f"Self-repair daemon: {repair_res.get('status')}")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _handle_sigterm(None, None)

if __name__ == "__main__":
    main()
