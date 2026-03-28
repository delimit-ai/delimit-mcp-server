"""
Inbox polling daemon for Delimit's email governance system.

Polls pro@delimit.ai via IMAP every 5 minutes, auto-classifies emails,
forwards owner-action items, and handles draft approval via email replies.

Consensus 116: Standalone daemon, fresh IMAP connections, 10-minute cancel window.

Can run as:
  - Standalone script: python inbox_daemon.py
  - MCP tool: delimit_inbox_daemon(action="start"|"stop"|"status")
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.inbox_daemon")

# ── Configuration ────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("DELIMIT_INBOX_POLL_INTERVAL", "300"))  # seconds
CANCEL_WINDOW = 600  # 10 minutes in seconds
MAX_CONSECUTIVE_FAILURES = 3

ALERTS_DIR = Path.home() / ".delimit" / "alerts"
ALERT_FILE = ALERTS_DIR / "inbox_daemon.json"
ROUTING_LOG = Path.home() / ".delimit" / "inbox_routing.jsonl"

# Approval keywords (case-insensitive, must be standalone words)
APPROVAL_KEYWORDS = [
    "approved",
    "approve",
    "yes",
    "go ahead",
    "lgtm",
    "looks good",
    "ship it",
    "post it",
]

CANCEL_KEYWORDS = [
    "cancel",
    "stop",
    "abort",
    "don't post",
    "do not post",
    "hold",
]

# Regex to extract 12-char hex draft ID from subject lines
DRAFT_ID_PATTERN = re.compile(r"\b([0-9a-f]{12})\b", re.IGNORECASE)


# ── Daemon State ─────────────────────────────────────────────────────

class InboxDaemonState:
    """Thread-safe state for the inbox polling daemon."""

    def __init__(self):
        self.running = False
        self.last_poll: Optional[str] = None
        self.items_processed: int = 0
        self.items_forwarded: int = 0
        self.approvals_detected: int = 0
        self.consecutive_failures: int = 0
        self.total_polls: int = 0
        self.stopped_reason: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Pending approvals: draft_id -> {approved_at, message_id, draft_record}
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_poll": self.last_poll,
                "items_processed": self.items_processed,
                "items_forwarded": self.items_forwarded,
                "approvals_detected": self.approvals_detected,
                "consecutive_failures": self.consecutive_failures,
                "total_polls": self.total_polls,
                "stopped_reason": self.stopped_reason,
                "pending_approvals": list(self._pending_approvals.keys()),
                "poll_interval_seconds": POLL_INTERVAL,
            }

    def record_success(self, processed: int, forwarded: int):
        with self._lock:
            self.consecutive_failures = 0
            self.items_processed += processed
            self.items_forwarded += forwarded
            self.total_polls += 1
            self.last_poll = datetime.now(timezone.utc).isoformat()

    def record_failure(self) -> int:
        with self._lock:
            self.consecutive_failures += 1
            self.total_polls += 1
            self.last_poll = datetime.now(timezone.utc).isoformat()
            return self.consecutive_failures

    def add_pending_approval(self, draft_id: str, info: Dict[str, Any]):
        with self._lock:
            self._pending_approvals[draft_id] = info
            self.approvals_detected += 1

    def remove_pending_approval(self, draft_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._pending_approvals.pop(draft_id, None)

    def get_pending_approvals(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._pending_approvals)


# Singleton state
_daemon_state = InboxDaemonState()


# ── Logging ──────────────────────────────────────────────────────────

def _log_routing(entry: Dict[str, Any]) -> None:
    """Append a routing decision to the audit trail."""
    try:
        ROUTING_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry["daemon"] = True
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(ROUTING_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to write routing log: %s", e)


def _write_alert(reason: str, failure_count: int) -> None:
    """Write an alert file for SessionStart to pick up."""
    try:
        ALERTS_DIR.mkdir(parents=True, exist_ok=True)
        alert = {
            "alert": "inbox_daemon_stopped",
            "reason": reason,
            "failure_count": failure_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(alert, f, indent=2)
        logger.error("Inbox daemon alert written: %s", reason)
    except OSError as e:
        logger.error("Failed to write alert file: %s", e)


# ── Approval Detection ───────────────────────────────────────────────

def detect_approval_keywords(text: str) -> bool:
    """Check if text contains approval keywords."""
    if not text:
        return False
    text_lower = text.lower().strip()
    for keyword in APPROVAL_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


def detect_cancel_keywords(text: str) -> bool:
    """Check if text contains cancellation keywords."""
    if not text:
        return False
    text_lower = text.lower().strip()
    for keyword in CANCEL_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


def _get_all_drafts() -> list:
    """Load all drafts via ai.social module. Separate function for clean mocking."""
    import ai.social
    return ai.social._load_all_drafts()


def _approve_draft(draft_id: str) -> dict:
    """Approve and post a draft via ai.social module. Separate function for clean mocking."""
    import ai.social
    return ai.social.approve_draft(draft_id)


def match_draft_by_headers(
    in_reply_to: str,
    references: str,
    subject: str,
) -> Optional[str]:
    """Match an inbound email to a draft via headers or subject line.

    Primary: In-Reply-To / References header matching against stored Message-IDs.
    Fallback: Extract 12-char hex draft ID from subject line.

    Returns draft_id if matched, None otherwise.
    """
    all_drafts = _get_all_drafts()

    # Primary: match In-Reply-To or References against stored notification_message_id
    if in_reply_to or references:
        header_ids = set()
        if in_reply_to:
            header_ids.add(in_reply_to.strip().strip("<>"))
        if references:
            for ref in references.split():
                header_ids.add(ref.strip().strip("<>"))

        for draft in all_drafts:
            stored_mid = draft.get("notification_message_id", "")
            if stored_mid and stored_mid.strip("<>") in header_ids:
                if draft.get("status") in ("pending", "approved-pending"):
                    return draft.get("draft_id")

    # Fallback: extract draft ID from subject
    # Strip Re:/Fwd: prefixes
    clean_subject = re.sub(r"^(Re|Fwd|Fw)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()
    match = DRAFT_ID_PATTERN.search(clean_subject)
    if match:
        candidate_id = match.group(1).lower()
        for draft in all_drafts:
            if draft.get("draft_id") == candidate_id:
                if draft.get("status") in ("pending", "approved-pending"):
                    return candidate_id

    return None


def mark_draft_status(draft_id: str, status: str) -> bool:
    """Update a draft's status without posting it.

    Used for approved-pending, cancelled states.
    """
    import ai.social

    all_drafts = ai.social._load_all_drafts()
    for draft in all_drafts:
        if draft.get("draft_id") == draft_id:
            draft["status"] = status
            draft[f"{status.replace('-', '_')}_at"] = datetime.now(timezone.utc).isoformat()
            ai.social._rewrite_drafts(all_drafts)
            return True
    return False


# ── Email Body Extraction ────────────────────────────────────────────

def _extract_body(msg) -> str:
    """Extract plain text body from an email message."""
    import email as _email

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


# ── Core Polling Logic ───────────────────────────────────────────────

def poll_once() -> Dict[str, Any]:
    """Execute a single poll cycle.

    1. Connect to IMAP, fetch unseen messages
    2. Classify each message
    3. Check for approval/cancel replies
    4. Forward owner-action emails
    5. Process pending approval windows

    Returns summary of actions taken.
    """
    import email as _email
    import email.header
    import email.utils
    import imaplib

    from ai.notify import (
        IMAP_HOST, IMAP_PORT, IMAP_USER, FORWARD_TO,
        classify_email, _extract_sender_email, _decode_header,
        _forward_email, send_email, _record_inbox_routing,
    )

    smtp_pass = os.environ.get("DELIMIT_SMTP_PASS", "")
    if not smtp_pass:
        return {"error": "DELIMIT_SMTP_PASS not set"}

    # Fresh IMAP connection each poll (consensus: no connection pooling)
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(IMAP_USER, smtp_pass)
    except Exception as e:
        failures = _daemon_state.record_failure()
        _log_routing({"event": "imap_failure", "error": str(e), "consecutive": failures})
        if failures >= MAX_CONSECUTIVE_FAILURES:
            reason = f"3 consecutive IMAP failures. Last: {e}"
            _daemon_state.stopped_reason = reason
            _daemon_state.running = False
            _daemon_state._stop_event.set()
            _write_alert(reason, failures)
        return {"error": f"IMAP connection failed: {e}", "consecutive_failures": failures}

    try:
        imap.select("INBOX")
        _, unseen_data = imap.search(None, "UNSEEN")
        unseen_ids = unseen_data[0].split() if unseen_data[0] else []

        processed = 0
        forwarded = 0
        approvals = []
        cancels = []

        for msg_id in unseen_ids[-20:]:  # Cap at 20 per cycle
            _, data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if not data or not data[0]:
                continue

            raw_email = data[0][1]
            msg = _email.message_from_bytes(raw_email)

            from_header = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            sender_addr = _extract_sender_email(from_header)
            in_reply_to = msg.get("In-Reply-To", "")
            references = msg.get("References", "")
            body_text = _extract_body(msg)

            classification = classify_email(sender_addr, subject, from_header)

            # Check for approval/cancel replies
            draft_id = match_draft_by_headers(in_reply_to, references, subject)

            action_taken = "classified"

            if draft_id and detect_cancel_keywords(body_text):
                # Cancel a pending approval
                pending = _daemon_state.remove_pending_approval(draft_id)
                if pending:
                    mark_draft_status(draft_id, "cancelled")
                    send_email(
                        to=FORWARD_TO,
                        subject=f"Draft {draft_id} CANCELLED",
                        body=f"Draft {draft_id} was cancelled via email reply. It will NOT be posted.",
                        from_account="pro@delimit.ai",
                        event_type="draft_cancelled",
                    )
                    action_taken = "cancel_received"
                    cancels.append(draft_id)
                else:
                    action_taken = "cancel_no_pending"
                # Mark as seen
                imap.store(msg_id, "+FLAGS", "\\Seen")

            elif draft_id and detect_approval_keywords(body_text):
                # Both draft match AND approval keyword required (consensus)
                mark_draft_status(draft_id, "approved-pending")
                _daemon_state.add_pending_approval(draft_id, {
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                    "approved_at_ts": time.time(),
                })
                # Send cancel-window notification
                send_email(
                    to=FORWARD_TO,
                    subject=f"Draft {draft_id} approved - posting in 10 minutes",
                    body=(
                        f"Draft {draft_id} has been approved via email reply.\n\n"
                        f"It will be posted in 10 minutes.\n\n"
                        f"Reply CANCEL to this email to stop the post."
                    ),
                    from_account="pro@delimit.ai",
                    event_type="draft_approval_window",
                )
                action_taken = "approval_detected"
                approvals.append(draft_id)
                # Mark as seen
                imap.store(msg_id, "+FLAGS", "\\Seen")

            elif classification == "owner-action":
                success = _forward_email(msg, smtp_pass)
                if success:
                    imap.store(msg_id, "+FLAGS", "\\Seen")
                    forwarded += 1
                action_taken = "forwarded" if success else "forward_failed"

            else:
                # Non-owner, mark as seen
                imap.store(msg_id, "+FLAGS", "\\Seen")
                action_taken = "non_owner_archived"

            processed += 1
            _log_routing({
                "event": "email_processed",
                "from": from_header,
                "sender": sender_addr,
                "subject": subject,
                "classification": classification,
                "draft_match": draft_id,
                "action": action_taken,
            })

        imap.logout()

        # Process pending approval windows (post drafts past the cancel window)
        posted_drafts = _process_pending_approvals()

        _daemon_state.record_success(processed, forwarded)

        return {
            "processed": processed,
            "forwarded": forwarded,
            "approvals_detected": approvals,
            "cancels_received": cancels,
            "drafts_posted": posted_drafts,
            "unseen_count": len(unseen_ids),
        }

    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        failures = _daemon_state.record_failure()
        _log_routing({"event": "poll_error", "error": str(e), "consecutive": failures})
        return {"error": f"Poll failed: {e}", "consecutive_failures": failures}


def _process_pending_approvals() -> List[str]:
    """Check pending approvals and post any past the cancel window."""

    posted = []
    now = time.time()
    pending = _daemon_state.get_pending_approvals()

    for draft_id, info in pending.items():
        approved_ts = info.get("approved_at_ts", 0)
        if now - approved_ts >= CANCEL_WINDOW:
            # Cancel window expired, post it
            _daemon_state.remove_pending_approval(draft_id)
            # Update status to approved so approve_draft can post it
            mark_draft_status(draft_id, "pending")  # Reset to pending for approve_draft
            result = _approve_draft(draft_id)
            if "error" not in result:
                posted.append(draft_id)
                _log_routing({
                    "event": "draft_auto_posted",
                    "draft_id": draft_id,
                    "post_result": result,
                })
            else:
                _log_routing({
                    "event": "draft_post_failed",
                    "draft_id": draft_id,
                    "error": result.get("error"),
                })

    return posted


# ── Daemon Loop ──────────────────────────────────────────────────────

def _daemon_loop() -> None:
    """Main polling loop. Runs until stop event is set."""
    logger.info("Inbox daemon started. Polling every %d seconds.", POLL_INTERVAL)
    _log_routing({"event": "daemon_started", "poll_interval": POLL_INTERVAL})

    while not _daemon_state._stop_event.is_set():
        try:
            result = poll_once()
            if "error" in result:
                logger.warning("Poll error: %s", result["error"])
            else:
                logger.info(
                    "Poll complete: %d processed, %d forwarded",
                    result.get("processed", 0),
                    result.get("forwarded", 0),
                )
        except Exception as e:
            logger.error("Unexpected error in daemon loop: %s", e)
            failures = _daemon_state.record_failure()
            if failures >= MAX_CONSECUTIVE_FAILURES:
                reason = f"3 consecutive failures in loop. Last: {e}"
                _daemon_state.stopped_reason = reason
                _write_alert(reason, failures)
                break

        # Wait for stop event or poll interval
        _daemon_state._stop_event.wait(timeout=POLL_INTERVAL)

    _daemon_state.running = False
    _log_routing({"event": "daemon_stopped", "reason": _daemon_state.stopped_reason or "manual_stop"})
    logger.info("Inbox daemon stopped.")


def start_daemon() -> Dict[str, Any]:
    """Start the inbox polling daemon in a background thread."""
    if _daemon_state.running:
        return {"status": "already_running", **_daemon_state.to_dict()}

    _daemon_state.running = True
    _daemon_state.stopped_reason = None
    _daemon_state.consecutive_failures = 0
    _daemon_state._stop_event.clear()

    thread = threading.Thread(target=_daemon_loop, name="inbox-daemon", daemon=True)
    _daemon_state._thread = thread
    thread.start()

    return {"status": "started", **_daemon_state.to_dict()}


def stop_daemon() -> Dict[str, Any]:
    """Stop the inbox polling daemon."""
    if not _daemon_state.running:
        return {"status": "not_running", **_daemon_state.to_dict()}

    _daemon_state._stop_event.set()
    _daemon_state.stopped_reason = "manual_stop"
    # Give the thread a moment to finish
    if _daemon_state._thread:
        _daemon_state._thread.join(timeout=5)
    _daemon_state.running = False

    return {"status": "stopped", **_daemon_state.to_dict()}


def get_daemon_status() -> Dict[str, Any]:
    """Get current daemon status."""
    result = _daemon_state.to_dict()

    # Check for alert file
    if ALERT_FILE.exists():
        try:
            with open(ALERT_FILE, "r") as f:
                result["alert"] = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    return result


# ── Standalone Entry Point ───────────────────────────────────────────

def main():
    """Run the daemon as a standalone process (for systemd).

    Supports argparse flags:
      --interval N   Override poll interval in seconds (default: DELIMIT_INBOX_POLL_INTERVAL or 300)
      --once         Run a single poll cycle and exit (useful for cron or testing)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Delimit inbox polling daemon — email governance for pro@delimit.ai",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Poll interval in seconds (default: DELIMIT_INBOX_POLL_INTERVAL env or 300)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not os.environ.get("DELIMIT_SMTP_PASS"):
        logger.error("DELIMIT_SMTP_PASS environment variable is required.")
        raise SystemExit(1)

    # Override poll interval if --interval flag provided
    global POLL_INTERVAL
    if args.interval is not None:
        POLL_INTERVAL = args.interval
        logger.info("Poll interval overridden to %d seconds via --interval flag.", POLL_INTERVAL)

    if args.once:
        logger.info("Running single poll cycle (--once mode)")
        result = poll_once()
        if "error" in result:
            logger.error("Poll failed: %s", result["error"])
            raise SystemExit(1)
        logger.info(
            "Poll complete: %d processed, %d forwarded",
            result.get("processed", 0),
            result.get("forwarded", 0),
        )
        return

    logger.info("Starting Delimit inbox polling daemon (standalone mode)")
    _daemon_state.running = True
    _daemon_state._stop_event.clear()

    try:
        _daemon_loop()
    except KeyboardInterrupt:
        logger.info("Interrupted. Shutting down.")
        _daemon_state._stop_event.set()
        _daemon_state.running = False


if __name__ == "__main__":
    main()
