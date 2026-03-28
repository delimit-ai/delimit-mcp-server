"""
Notification helper for delimit_notify and delimit_notify_inbox tools.

Supports webhook, Slack, and email channels (outbound).
Supports impact-based notification routing (LED-233).
Supports IMAP inbox polling with classification and forwarding (inbound).
Stores notification history in ~/.delimit/notifications.jsonl.
Stores inbox routing log in ~/.delimit/inbox_routing.jsonl.
"""

import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger("delimit.ai.notify")

HISTORY_FILE = Path.home() / ".delimit" / "notifications.jsonl"
INBOX_ROUTING_FILE = Path.home() / ".delimit" / "inbox_routing.jsonl"

# ── Inbound email configuration ──────────────────────────────────────
IMAP_HOST = "mail.spacemail.com"
IMAP_PORT = 993
IMAP_USER = "pro@delimit.ai"
FORWARD_TO = "configured-email@example.com"

# Domains/senders whose emails require owner action
OWNER_ACTION_DOMAINS = {
    "cooperpress.com",
    "github.com",
    "lemon.com",
    "lemonsqueezy.com",
    "namecheap.com",
    "stripe.com",
    "google.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "npmjs.com",
    "vercel.com",
    "supabase.io",
    "supabase.com",
    "glama.ai",
    "vultr.com",
    "digitalocean.com",
}

OWNER_ACTION_SENDERS = {
    "configured-email@example.com",
}

# Subject patterns that indicate owner-action (compiled once)
import re as _re
OWNER_ACTION_SUBJECT_PATTERNS = [
    _re.compile(r"social\s+draft", _re.IGNORECASE),
    _re.compile(r"show\s+hn", _re.IGNORECASE),
    _re.compile(r"approval", _re.IGNORECASE),
    _re.compile(r"action\s+required", _re.IGNORECASE),
    _re.compile(r"reply|respond", _re.IGNORECASE),
    _re.compile(r"invoice", _re.IGNORECASE),
    _re.compile(r"payment", _re.IGNORECASE),
    _re.compile(r"subscription", _re.IGNORECASE),
]

# Sender patterns that are definitely non-owner (automated/bot)
NON_OWNER_SENDERS = {
    "noreply@",
    "no-reply@",
    "notifications@",
    "mailer-daemon@",
    "postmaster@",
    "donotreply@",
}


def _record_notification(entry: Dict[str, Any]) -> None:
    """Append a notification record to the history file."""
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to record notification: %s", e)


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 10) -> Dict[str, Any]:
    """POST a JSON payload to a URL. Returns status dict."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "status_code": resp.status,
                "success": 200 <= resp.status < 300,
            }
    except urllib.error.HTTPError as e:
        return {"status_code": e.code, "success": False, "error": str(e)}
    except urllib.error.URLError as e:
        return {"status_code": 0, "success": False, "error": str(e)}


def send_webhook(
    webhook_url: str,
    message: str,
    event_type: str = "",
) -> Dict[str, Any]:
    """Send a generic webhook notification (JSON POST)."""
    if not webhook_url:
        return {"error": "webhook_url is required for webhook channel"}

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_type": event_type or "delimit_notification",
        "message": message,
        "timestamp": timestamp,
    }

    result = _post_json(webhook_url, payload)
    record = {
        "channel": "webhook",
        "event_type": event_type,
        "message": message,
        "webhook_url": webhook_url,
        "timestamp": timestamp,
        "success": result.get("success", False),
    }
    _record_notification(record)

    return {
        "channel": "webhook",
        "delivered": result.get("success", False),
        "status_code": result.get("status_code"),
        "timestamp": timestamp,
        "error": result.get("error"),
    }


def send_slack(
    webhook_url: str,
    message: str,
    event_type: str = "",
) -> Dict[str, Any]:
    """Send a Slack notification via incoming webhook."""
    if not webhook_url:
        return {"error": "webhook_url is required for slack channel"}

    timestamp = datetime.now(timezone.utc).isoformat()
    prefix = f"[{event_type}] " if event_type else ""
    payload = {"text": f"{prefix}{message}"}

    result = _post_json(webhook_url, payload)
    record = {
        "channel": "slack",
        "event_type": event_type,
        "message": message,
        "webhook_url": webhook_url,
        "timestamp": timestamp,
        "success": result.get("success", False),
    }
    _record_notification(record)

    return {
        "channel": "slack",
        "delivered": result.get("success", False),
        "status_code": result.get("status_code"),
        "timestamp": timestamp,
        "error": result.get("error"),
    }


def _load_smtp_account(from_account: str) -> Optional[Dict[str, str]]:
    """Load SMTP credentials from smtp-all.json for a given account.

    Args:
        from_account: Email address key in smtp-all.json (e.g. 'pro@delimit.ai').

    Returns:
        Dict with host, port, user, pass keys, or None if not found.
    """
    secrets_path = Path.home() / ".delimit" / "secrets" / "smtp-all.json"
    try:
        if not secrets_path.exists():
            return None
        with open(secrets_path, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        if from_account in accounts:
            return accounts[from_account]
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load smtp-all.json: %s", e)
        return None


def send_email(
    to: str = "",
    subject: str = "",
    body: str = "",
    from_account: str = "",
    message: str = "",
    event_type: str = "",
) -> Dict[str, Any]:
    """Send an email notification via SMTP.

    Args:
        to: Recipient email address. Falls back to DELIMIT_SMTP_TO or
            configured-email@example.com.
        subject: Email subject line.
        body: Email body text (preferred). Falls back to 'message' for
            backward compatibility.
        from_account: Sender account key in ~/.delimit/secrets/smtp-all.json
            (e.g. 'pro@delimit.ai', 'admin@wire.report'). If provided, SMTP
            credentials are loaded from that file instead of env vars.
        message: Email body text (legacy parameter, use 'body' instead).
        event_type: Event category for filtering/logging.

    Credential resolution order:
        1. from_account lookup in ~/.delimit/secrets/smtp-all.json
        2. DELIMIT_SMTP_* environment variables
    """
    # body takes precedence, fall back to message for backward compat
    email_body = body or message

    timestamp = datetime.now(timezone.utc).isoformat()

    # Try from_account first, then fall back to env vars
    account_creds = _load_smtp_account(from_account) if from_account else None

    if account_creds:
        smtp_host = account_creds.get("host", "")
        smtp_port = int(account_creds.get("port", 587))
        smtp_user = account_creds.get("user", "")
        smtp_pass = account_creds.get("pass", "")
        smtp_from = from_account
    else:
        smtp_host = os.environ.get("DELIMIT_SMTP_HOST", "")
        smtp_port = int(os.environ.get("DELIMIT_SMTP_PORT", "587"))
        smtp_user = os.environ.get("DELIMIT_SMTP_USER", "")
        smtp_pass = os.environ.get("DELIMIT_SMTP_PASS", "")
        smtp_from = os.environ.get("DELIMIT_SMTP_FROM", "")

    smtp_to = to or os.environ.get("DELIMIT_SMTP_TO", "configured-email@example.com")

    if not all([smtp_host, smtp_from, smtp_to]):
        record = {
            "channel": "email",
            "event_type": event_type,
            "to": smtp_to,
            "from": smtp_from,
            "message": email_body,
            "subject": subject,
            "timestamp": timestamp,
            "success": False,
            "reason": "smtp_not_configured",
        }
        _record_notification(record)
        return {
            "channel": "email",
            "delivered": False,
            "timestamp": timestamp,
            "error": "SMTP not configured. Set DELIMIT_SMTP_HOST, DELIMIT_SMTP_FROM, DELIMIT_SMTP_TO environment variables, or use from_account with smtp-all.json.",
            "intent_logged": True,
        }

    subj = subject or f"Delimit: {event_type or 'Notification'}"
    msg = MIMEText(email_body)
    msg["Subject"] = subj
    msg["From"] = smtp_from
    msg["To"] = smtp_to

    # Generate a unique Message-ID for threading support (Consensus 116)
    import uuid as _uuid
    domain = smtp_from.split("@", 1)[1] if "@" in smtp_from else "delimit.ai"
    message_id = f"<{_uuid.uuid4().hex}@{domain}>"
    msg["Message-ID"] = message_id

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if smtp_user and smtp_pass:
                server.starttls()
                server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [smtp_to], msg.as_string())

        record = {
            "channel": "email",
            "event_type": event_type,
            "to": smtp_to,
            "from": smtp_from,
            "subject": subj,
            "message": email_body,
            "timestamp": timestamp,
            "success": True,
            "message_id": message_id,
        }
        _record_notification(record)

        return {
            "channel": "email",
            "delivered": True,
            "timestamp": timestamp,
            "subject": subj,
            "to": smtp_to,
            "from": smtp_from,
            "message_id": message_id,
        }
    except Exception as e:
        record = {
            "channel": "email",
            "event_type": event_type,
            "to": smtp_to,
            "from": smtp_from,
            "message": email_body,
            "timestamp": timestamp,
            "success": False,
            "error": str(e),
        }
        _record_notification(record)
        return {
            "channel": "email",
            "delivered": False,
            "timestamp": timestamp,
            "error": str(e),
        }


def send_notification(
    channel: str = "webhook",
    message: str = "",
    webhook_url: str = "",
    subject: str = "",
    event_type: str = "",
    to: str = "",
    from_account: str = "",
) -> Dict[str, Any]:
    """Route a notification to the appropriate channel."""
    if not message:
        return {"error": "message is required"}

    if channel == "webhook":
        return send_webhook(webhook_url, message, event_type)
    elif channel == "slack":
        return send_slack(webhook_url, message, event_type)
    elif channel == "email":
        return send_email(
            to=to,
            subject=subject,
            message=message,
            from_account=from_account,
            event_type=event_type,
        )
    else:
        return {"error": f"Unknown channel: {channel}. Supported: webhook, slack, email"}


# ═════════════════════════════════════════════════════════════════════
#  LED-233: Impact-Based Notification Routing
# ═════════════════════════════════════════════════════════════════════

ROUTING_CONFIG_FILE = Path.home() / ".delimit" / "notify_routing.yaml"

# Severity aliases — map various input labels to canonical levels
_SEVERITY_ALIASES: Dict[str, str] = {
    "critical": "critical",
    "breaking": "critical",
    "error": "critical",
    "major": "critical",
    "warning": "warning",
    "non-breaking": "warning",
    "minor": "warning",
    "info": "info",
    "cosmetic": "info",
    "docs": "info",
    "patch": "info",
    "none": "info",
}

DEFAULT_ROUTING_CONFIG: Dict[str, Any] = {
    "routing": {
        "critical": {
            "channels": ["email", "webhook"],
            "email_subject_prefix": "[URGENT]",
            "webhook_priority": "high",
        },
        "warning": {
            "channels": ["webhook"],
            "webhook_priority": "normal",
        },
        "info": {
            "channels": [],
            "digest": True,
        },
    },
}


def load_routing_config() -> Dict[str, Any]:
    """Load routing config from ~/.delimit/notify_routing.yaml or return defaults.

    Returns:
        The routing configuration dict with a 'routing' key.
    """
    if ROUTING_CONFIG_FILE.exists():
        try:
            if _yaml is not None:
                with open(ROUTING_CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = _yaml.safe_load(f)
                if isinstance(config, dict) and "routing" in config:
                    return config
            else:
                # Fallback: try JSON (yaml not installed)
                with open(ROUTING_CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if isinstance(config, dict) and "routing" in config:
                    return config
        except Exception as e:
            logger.warning("Failed to load routing config from %s: %s", ROUTING_CONFIG_FILE, e)
    return DEFAULT_ROUTING_CONFIG


def save_routing_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Save routing config to ~/.delimit/notify_routing.yaml.

    Args:
        config: Full routing config dict (must contain 'routing' key).

    Returns:
        Status dict with success/error.
    """
    if "routing" not in config:
        return {"error": "Config must contain a 'routing' key."}
    try:
        ROUTING_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _yaml is not None:
            with open(ROUTING_CONFIG_FILE, "w", encoding="utf-8") as f:
                _yaml.dump(config, f, default_flow_style=False)
        else:
            with open(ROUTING_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        return {"success": True, "path": str(ROUTING_CONFIG_FILE)}
    except Exception as e:
        return {"error": f"Failed to save routing config: {e}"}


def _classify_severity(change: Dict[str, Any]) -> str:
    """Map a single change dict to a canonical severity level (critical/warning/info).

    Inspects these keys in order:
      - 'severity' (from lint violations)
      - 'is_breaking' (from diff changes)
      - 'type' (change type string)
    """
    # Direct severity label
    sev = str(change.get("severity", "")).lower()
    if sev in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[sev]

    # Breaking flag from diff engine
    if change.get("is_breaking"):
        return "critical"

    # Change type heuristic
    ctype = str(change.get("type", "")).lower()
    if "removed" in ctype or "breaking" in ctype:
        return "critical"
    if "added" in ctype or "changed" in ctype:
        return "warning"

    return "info"


def route_by_impact(
    changes: List[Dict[str, Any]],
    routing_config: Optional[Dict[str, Any]] = None,
    webhook_url: str = "",
    email_to: str = "",
    from_account: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Route notifications based on change severity.

    Takes a list of changes (from lint/diff output) and sends notifications
    to the appropriate channels based on severity classification.

    Args:
        changes: List of change dicts (from lint violations or diff changes).
        routing_config: Custom routing config. Uses saved/default if None.
        webhook_url: Webhook URL for webhook channel delivery.
        email_to: Email recipient for email channel delivery.
        from_account: Sender account key for email delivery.
        dry_run: If True, classify and plan routing but do not send.

    Returns:
        Dict with routing decisions and delivery results.
    """
    if not changes:
        return {
            "routed": 0,
            "suppressed": 0,
            "decisions": [],
            "notifications_sent": [],
        }

    config = routing_config or load_routing_config()
    routing_rules = config.get("routing", {})
    timestamp = datetime.now(timezone.utc).isoformat()

    # Classify all changes by severity
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "critical": [],
        "warning": [],
        "info": [],
    }
    for change in changes:
        severity = _classify_severity(change)
        buckets[severity].append(change)

    decisions: List[Dict[str, Any]] = []
    notifications_sent: List[Dict[str, Any]] = []
    suppressed_count = 0

    for severity, items in buckets.items():
        if not items:
            continue

        rule = routing_rules.get(severity, {})
        channels = rule.get("channels", [])
        is_digest = rule.get("digest", False)

        if not channels:
            # Suppressed (or digest-only)
            suppressed_count += len(items)
            decisions.append({
                "severity": severity,
                "count": len(items),
                "action": "digest" if is_digest else "suppressed",
                "channels": [],
            })
            continue

        # Build notification message for this severity bucket
        subject_prefix = rule.get("email_subject_prefix", "")
        webhook_priority = rule.get("webhook_priority", "normal")

        summary_lines = [f"{len(items)} {severity} change(s) detected:"]
        for item in items[:10]:  # Cap detail lines at 10
            msg = item.get("message", item.get("name", item.get("type", "change")))
            path = item.get("path", "")
            summary_lines.append(f"  - {msg}" + (f" ({path})" if path else ""))
        if len(items) > 10:
            summary_lines.append(f"  ... and {len(items) - 10} more")
        message_body = "\n".join(summary_lines)

        decision = {
            "severity": severity,
            "count": len(items),
            "action": "notify",
            "channels": list(channels),
        }
        decisions.append(decision)

        if dry_run:
            continue

        # Send to each configured channel
        for channel in channels:
            if channel == "email":
                subject = f"{subject_prefix} Delimit: {severity} API changes".strip()
                result = send_email(
                    to=email_to,
                    subject=subject,
                    body=message_body,
                    from_account=from_account,
                    event_type=f"impact_routing_{severity}",
                )
                notifications_sent.append({
                    "channel": "email",
                    "severity": severity,
                    "delivered": result.get("delivered", False),
                    "error": result.get("error"),
                })
            elif channel == "webhook" and webhook_url:
                # Inject priority into the webhook payload
                payload = {
                    "event_type": f"delimit_impact_{severity}",
                    "message": message_body,
                    "priority": webhook_priority,
                    "severity": severity,
                    "change_count": len(items),
                    "timestamp": timestamp,
                }
                post_result = _post_json(webhook_url, payload)
                _record_notification({
                    "channel": "webhook",
                    "event_type": f"impact_routing_{severity}",
                    "message": message_body,
                    "webhook_url": webhook_url,
                    "priority": webhook_priority,
                    "timestamp": timestamp,
                    "success": post_result.get("success", False),
                })
                notifications_sent.append({
                    "channel": "webhook",
                    "severity": severity,
                    "priority": webhook_priority,
                    "delivered": post_result.get("success", False),
                    "error": post_result.get("error"),
                })
            elif channel == "slack" and webhook_url:
                result = send_slack(webhook_url, message_body, f"impact_{severity}")
                notifications_sent.append({
                    "channel": "slack",
                    "severity": severity,
                    "delivered": result.get("delivered", False),
                    "error": result.get("error"),
                })

    return {
        "routed": sum(d["count"] for d in decisions if d["action"] == "notify"),
        "suppressed": suppressed_count,
        "decisions": decisions,
        "notifications_sent": notifications_sent,
        "timestamp": timestamp,
        "dry_run": dry_run,
    }


# ═════════════════════════════════════════════════════════════════════
#  INBOUND EMAIL: IMAP polling, classification, and forwarding
# ═════════════════════════════════════════════════════════════════════

def _record_inbox_routing(entry: Dict[str, Any]) -> None:
    """Append a routing record to the inbox routing log."""
    try:
        INBOX_ROUTING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(INBOX_ROUTING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to record inbox routing: %s", e)


def _decode_header(raw: str) -> str:
    """Decode an RFC 2047 encoded email header into a plain string."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _extract_sender_email(from_header: str) -> str:
    """Extract the bare email address from a From header."""
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower()


def _extract_sender_domain(sender_email: str) -> str:
    """Extract domain from an email address."""
    if "@" in sender_email:
        return sender_email.split("@", 1)[1]
    return ""


def classify_email(sender: str, subject: str, from_header: str = "") -> str:
    """Classify an email as 'owner-action' or 'non-owner'.

    Returns:
        'owner-action' if the email needs owner attention.
        'non-owner' if it can stay in the Delimit inbox.
    """
    sender_lower = sender.lower()
    sender_domain = _extract_sender_domain(sender_lower)

    # Rule 1: from the owner directly
    if sender_lower in OWNER_ACTION_SENDERS:
        return "owner-action"

    # Rule 2: from a known vendor/partner domain
    if sender_domain in OWNER_ACTION_DOMAINS:
        return "owner-action"

    # Rule 3: subject matches owner-action patterns
    for pattern in OWNER_ACTION_SUBJECT_PATTERNS:
        if pattern.search(subject):
            return "owner-action"

    # Rule 4: if sender looks like a real person (not noreply), lean owner-action
    # Only if from_header has a display name that looks personal
    is_noreply = any(sender_lower.startswith(prefix) for prefix in NON_OWNER_SENDERS)
    if not is_noreply and sender_domain and sender_domain not in ("pypi.org",):
        # Check if subject indicates automated content
        automated_keywords = ["unsubscribe", "newsletter", "digest", "weekly roundup",
                              "notification", "alert", "automated", "receipt"]
        subject_lower = subject.lower()
        if any(kw in subject_lower for kw in automated_keywords):
            return "non-owner"
        # Personal email from unknown domain - forward to be safe
        return "owner-action"

    return "non-owner"


def _forward_email(original_msg: email.message.Message, smtp_pass: str) -> bool:
    """Forward an email to the owner via SMTP."""
    subject = _decode_header(original_msg.get("Subject", ""))
    from_header = original_msg.get("From", "")

    # Build forwarded message
    body_parts = []
    if original_msg.is_multipart():
        for part in original_msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = original_msg.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode("utf-8", errors="replace"))

    body = "\n".join(body_parts) if body_parts else "(no text content)"

    fwd_text = (
        f"--- Forwarded from pro@delimit.ai ---\n"
        f"From: {from_header}\n"
        f"Subject: {subject}\n"
        f"Date: {original_msg.get('Date', 'unknown')}\n"
        f"---\n\n"
        f"{body}"
    )

    fwd_msg = MIMEText(fwd_text)
    fwd_msg["Subject"] = f"[Fwd] {subject}"
    fwd_msg["From"] = IMAP_USER
    fwd_msg["To"] = FORWARD_TO

    try:
        with smtplib.SMTP(IMAP_HOST, 587, timeout=10) as server:
            server.starttls()
            server.login(IMAP_USER, smtp_pass)
            server.sendmail(IMAP_USER, [FORWARD_TO], fwd_msg.as_string())
        return True
    except Exception as e:
        logger.error("Failed to forward email: %s", e)
        return False


def poll_inbox(
    smtp_pass: str = "",
    limit: int = 20,
    process: bool = True,
) -> Dict[str, Any]:
    """Poll the IMAP inbox, classify emails, and optionally forward owner-action items.

    Args:
        smtp_pass: SMTP/IMAP password for pro@delimit.ai.
        limit: Max number of recent messages to check.
        process: If True, forward owner-action emails and mark as read.
                 If False, just report classification (dry run).

    Returns:
        Summary of inbox state and routing decisions.
    """
    if not smtp_pass:
        smtp_pass = os.environ.get("DELIMIT_SMTP_PASS", "")
    if not smtp_pass:
        return {"error": "IMAP password required. Set DELIMIT_SMTP_PASS or pass smtp_pass."}

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(IMAP_USER, smtp_pass)
    except Exception as e:
        return {"error": f"IMAP connection failed: {e}"}

    try:
        imap.select("INBOX")

        # Get UNSEEN messages first, then fall back to recent ALL
        status, unseen_data = imap.search(None, "UNSEEN")
        unseen_ids = unseen_data[0].split() if unseen_data[0] else []

        # Also get all message IDs for the summary
        status, all_data = imap.search(None, "ALL")
        all_ids = all_data[0].split() if all_data[0] else []

        # Process unseen messages (up to limit)
        target_ids = unseen_ids[-limit:] if unseen_ids else []

        results: List[Dict[str, Any]] = []
        forwarded = 0
        skipped = 0

        for msg_id in target_ids:
            # Fetch without marking as seen (use BODY.PEEK)
            status, data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not data or not data[0]:
                continue

            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            from_header = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            date_str = msg.get("Date", "")
            sender_addr = _extract_sender_email(from_header)

            classification = classify_email(sender_addr, subject, from_header)

            entry = {
                "msg_id": msg_id.decode(),
                "from": from_header,
                "sender": sender_addr,
                "subject": subject,
                "date": date_str,
                "classification": classification,
                "forwarded": False,
            }

            if process and classification == "owner-action":
                success = _forward_email(msg, smtp_pass)
                entry["forwarded"] = success
                if success:
                    # Mark as seen after successful forward
                    imap.store(msg_id, "+FLAGS", "\\Seen")
                    forwarded += 1
                else:
                    entry["forward_error"] = True
            elif process and classification == "non-owner":
                # Mark as seen (processed, stays in inbox)
                imap.store(msg_id, "+FLAGS", "\\Seen")
                skipped += 1

            results.append(entry)
            _record_inbox_routing({**entry, "timestamp": timestamp, "process_mode": process})

        imap.logout()

        return {
            "timestamp": timestamp,
            "total_messages": len(all_ids),
            "unseen_count": len(unseen_ids),
            "processed": len(results),
            "forwarded_to_owner": forwarded,
            "kept_in_inbox": skipped,
            "dry_run": not process,
            "messages": results,
        }

    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return {"error": f"Inbox processing failed: {e}"}


def get_inbox_status(
    smtp_pass: str = "",
    limit: int = 10,
) -> Dict[str, Any]:
    """Get inbox status and recent routing history without processing.

    Args:
        smtp_pass: IMAP password.
        limit: Number of recent messages to show.

    Returns:
        Inbox summary and recent routing log entries.
    """
    # Get recent routing history from log
    routing_history: List[Dict[str, Any]] = []
    try:
        if INBOX_ROUTING_FILE.exists():
            with open(INBOX_ROUTING_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                try:
                    routing_history.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    # Get live inbox state
    if not smtp_pass:
        smtp_pass = os.environ.get("DELIMIT_SMTP_PASS", "")
    if not smtp_pass:
        return {
            "routing_history": routing_history,
            "error": "IMAP password required for live inbox status. Set DELIMIT_SMTP_PASS.",
        }

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(IMAP_USER, smtp_pass)
        imap.select("INBOX")

        _, all_data = imap.search(None, "ALL")
        all_ids = all_data[0].split() if all_data[0] else []

        _, unseen_data = imap.search(None, "UNSEEN")
        unseen_ids = unseen_data[0].split() if unseen_data[0] else []

        # Preview recent messages
        recent_ids = all_ids[-limit:]
        recent_msgs: List[Dict[str, str]] = []
        for msg_id in recent_ids:
            _, data = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
            if data and data[0]:
                # Parse flags and headers
                flags_str = ""
                header_bytes = b""
                for part in data:
                    if isinstance(part, tuple):
                        if b"FLAGS" in part[0]:
                            flags_str = part[0].decode(errors="replace")
                        header_bytes = part[1]

                header_text = header_bytes.decode("utf-8", errors="replace")
                tmp_msg = email.message_from_string(header_text)
                from_h = _decode_header(tmp_msg.get("From", ""))
                subj_h = _decode_header(tmp_msg.get("Subject", ""))
                date_h = tmp_msg.get("Date", "")
                sender = _extract_sender_email(from_h)
                cls = classify_email(sender, subj_h, from_h)
                seen = "\\Seen" in flags_str

                recent_msgs.append({
                    "from": from_h,
                    "subject": subj_h,
                    "date": date_h,
                    "classification": cls,
                    "seen": seen,
                })

        imap.logout()

        return {
            "total_messages": len(all_ids),
            "unseen_count": len(unseen_ids),
            "recent_messages": recent_msgs,
            "routing_history_count": len(routing_history),
            "routing_history": routing_history[-5:],
        }

    except Exception as e:
        return {
            "routing_history": routing_history,
            "error": f"IMAP connection failed: {e}",
        }
