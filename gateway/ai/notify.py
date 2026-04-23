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

import threading
import time as _time

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

# ── Email Throttle (Storm Prevention) ────────────────────────────────
# - Max 30 immediate emails per hour (raised from 5 — was too aggressive)
# - Only truly spammy patterns get batched (daemon auto-emails, scan digests)
# - All founder-initiated, approval, alert, and social draft emails send immediately
_email_throttle_lock = threading.Lock()
_email_send_times: list = []  # timestamps of recent sends
_email_digest_queue: list = []  # batched non-urgent emails
_EMAIL_MAX_PER_HOUR = 10
_EMAIL_DIGEST_INTERVAL = 3600  # 60 minutes (was 30 — reduce digest flood)
_last_digest_flush = 0.0

logger = logging.getLogger("delimit.ai.notify")

HISTORY_FILE = Path.home() / ".delimit" / "notifications.jsonl"
INBOX_ROUTING_FILE = Path.home() / ".delimit" / "inbox_routing.jsonl"
OWNER_ACTIONS_FILE = Path.home() / ".delimit" / "owner_actions.jsonl"

def _load_json_file(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_secret_value(*names: str) -> str:
    """Load a secret value from ~/.delimit/secrets/<NAME>.json files."""
    secrets_dir = Path.home() / ".delimit" / "secrets"
    for name in names:
        data = _load_json_file(secrets_dir / f"{name}.json")
        value = data.get("value") or data.get("token") or data.get("access_token") or ""
        if value:
            return str(value)
    return ""


def _load_inbound_email_config() -> Dict[str, str]:
    secrets_dir = Path.home() / ".delimit" / "secrets"
    smtp_accounts = _load_json_file(secrets_dir / "smtp-all.json")
    defaults = smtp_accounts.get("_defaults", {}) if isinstance(smtp_accounts.get("_defaults"), dict) else {}
    account_name = str(defaults.get("from_account") or "pro@delimit.ai")
    account = smtp_accounts.get(account_name, {}) if isinstance(smtp_accounts.get(account_name), dict) else {}
    forward_cfg = _load_json_file(secrets_dir / "forward-to.json")

    return {
        "imap_host": str(account.get("host") or ""),
        "imap_port": str(account.get("imap_port") or "993"),
        "imap_user": str(account.get("user") or account_name or ""),
        "forward_to": str(
            os.environ.get("DELIMIT_FORWARD_TO", "")
            or forward_cfg.get("value")
            or forward_cfg.get("to")
            or defaults.get("to")
            or ""
        ),
    }


# ── Inbound email configuration ──────────────────────────────────────
_INBOUND_CFG = _load_inbound_email_config()
IMAP_HOST = os.environ.get("DELIMIT_IMAP_HOST", "") or _INBOUND_CFG.get("imap_host", "")
IMAP_PORT = int(os.environ.get("DELIMIT_IMAP_PORT", "") or _INBOUND_CFG.get("imap_port", "993"))
IMAP_USER = os.environ.get("DELIMIT_IMAP_USER", "") or _INBOUND_CFG.get("imap_user", "")
FORWARD_TO = _INBOUND_CFG.get("forward_to", "")

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

OWNER_ACTION_SENDERS = set(
    filter(None, [os.environ.get("DELIMIT_OWNER_EMAIL", "")])
)

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


def record_owner_action(entry: Dict[str, Any]) -> None:
    """Append an owner-action record for dashboard and async fanout."""
    try:
        OWNER_ACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            **entry,
        }
        with open(OWNER_ACTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError as e:
        logger.warning("Failed to record owner action: %s", e)


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


def send_telegram(
    message: str,
    event_type: str = "",
    bot_token: str = "",
    chat_id: str = "",
) -> Dict[str, Any]:
    """Send a Telegram message via bot API."""
    bot_token = bot_token or os.environ.get("DELIMIT_TELEGRAM_BOT_TOKEN", "") or _load_secret_value("DELIMIT_TELEGRAM_BOT_TOKEN", "TELEGRAM_MONITOR_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("DELIMIT_TELEGRAM_CHAT_ID", "") or _load_secret_value("DELIMIT_TELEGRAM_CHAT_ID", "TELEGRAM_MONITOR_CHAT_ID")
    if not bot_token or not chat_id:
        return {"error": "telegram bot token and chat id are required"}

    timestamp = datetime.now(timezone.utc).isoformat()
    prefix = f"[{event_type}] " if event_type else ""
    payload = {
        "chat_id": chat_id,
        "text": f"{prefix}{message}",
        "disable_web_page_preview": False,
    }
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    result = _post_json(url, payload)
    _record_notification({
        "channel": "telegram",
        "event_type": event_type,
        "message": message,
        "timestamp": timestamp,
        "success": result.get("success", False),
    })
    return {
        "channel": "telegram",
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


def _flush_email_digest():
    """Send all queued non-urgent emails as a single HTML digest."""
    global _email_digest_queue
    if not _email_digest_queue:
        return

    items = list(_email_digest_queue)
    _email_digest_queue.clear()

    # Drop pure noise items — zero-action social scans, heartbeats
    noise_types = {"heartbeat", "scan_summary", "daemon_status", "digest_suppressed"}
    items = [i for i in items if i.get("event_type", "") not in noise_types]
    if not items:
        return

    # Rank items by priority: security/alert first, then actions, then drafts
    PRIORITY_ORDER = {
        "security": 0, "alert": 0, "gate_failure": 0,
        "deploy": 1, "action": 1, "approve": 1,
        "founder_directive": 2,
        "social_draft": 3, "github_outreach_queued": 3,
        "info": 4, "digest": 4,
        "daemon_status": 5, "scan_summary": 5, "heartbeat": 5,
    }
    items.sort(key=lambda x: PRIORITY_ORDER.get(x.get("event_type", ""), 4))

    # Build digest body — each item gets its own section
    # Social drafts get the full draft text in a copy block
    # Other items get a summary
    digest_text = f"{len(items)} notification{'s' if len(items) != 1 else ''} batched in this digest.\n\n"
    for i, item in enumerate(items, 1):
        subj = item.get('subject', 'No subject')
        body_raw = item.get('body', '')
        event = item.get('event_type', '')

        digest_text += f"{i}. {subj}\n\n"

        if event in ('social_draft', 'github_outreach_queued'):
            # Extract just the draft text from the full email body
            # Look for the copy block content between markers
            if '--- COPY BELOW THIS LINE ---' in body_raw:
                parts = body_raw.split('--- COPY BELOW THIS LINE ---', 1)
                before_copy = parts[0].strip()
                after_marker = parts[1] if len(parts) > 1 else ''
                # Get text before END COPY
                draft_only = after_marker.split('--- END COPY ---')[0].strip() if '--- END COPY ---' in after_marker else after_marker.strip()
                # Show context (WHERE/LINK/WHY) then copy block
                for line in before_copy.split('\n'):
                    line = line.strip()
                    if line.startswith(('WHERE:', 'LINK:', 'WHY:')):
                        digest_text += f"{line}\n"
                digest_text += f"\n--- COPY BELOW THIS LINE ---\n{draft_only}\n--- END COPY ---\n\n"
            else:
                digest_text += f"{body_raw[:500]}\n\n"
        else:
            digest_text += f"{body_raw[:500]}\n\n"

    digest_subject = f"[DIGEST] {len(items)} Delimit notifications"
    digest_html = _render_html_email(digest_subject, digest_text, "digest")

    # Send digest as a single email (bypasses throttle since it IS the flush)
    _email_send_times.append(_time.time())
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        from_acct = items[0].get("from_account", "") if items else ""
        smtp_to = items[0].get("to", "") if items else ""
        if not smtp_to:
            defaults = _load_smtp_account("_defaults") or {}
            smtp_to = str(defaults.get("to") or os.environ.get("DELIMIT_SMTP_TO", ""))

        # Resolve SMTP account: from_acct → _defaults.from_account → env vars
        acct = _load_smtp_account(from_acct) if from_acct else None
        if not acct or not acct.get("pass"):
            defaults = _load_smtp_account("_defaults") or {}
            fallback_acct = defaults.get("from_account", "")
            if fallback_acct:
                acct = _load_smtp_account(fallback_acct) or {}
            else:
                acct = defaults
        smtp_host = acct.get("host", os.environ.get("DELIMIT_SMTP_HOST", "smtp.gmail.com"))
        smtp_port = int(acct.get("port", os.environ.get("DELIMIT_SMTP_PORT", "587")))
        smtp_user = acct.get("user", os.environ.get("DELIMIT_SMTP_USER", ""))
        smtp_pass = acct.get("pass", os.environ.get("DELIMIT_SMTP_PASS", ""))
        smtp_from = acct.get("from", smtp_user)

        if not smtp_pass:
            logger.warning("Digest flush skipped: no SMTP password")
            return

        msg = MIMEText(digest_html, "html", "utf-8")
        msg["Subject"] = digest_subject
        msg["From"] = smtp_from
        msg["To"] = smtp_to

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [smtp_to], msg.as_string())
        delivered = True
        _record_notification({
            "channel": "email",
            "event_type": "digest",
            "to": smtp_to,
            "from": from_acct or "pro@delimit.ai",
            "subject": digest_subject,
            "message": digest_text,
            "timestamp": timestamp,
            "success": delivered,
            "items": len(items),
        })
        logger.info("Flushed email digest: %d items", len(items))
    except Exception as e:
        logger.warning("Digest flush failed: %s", e)


def _send_smtp_direct(to: str, subject: str, body: str, from_account: str = "") -> bool:
    """Low-level SMTP send — used by digest flush and direct sends."""
    if not from_account:
        defaults = _load_smtp_account("_defaults")
        if defaults and defaults.get("from_account"):
            from_account = str(defaults["from_account"])

    acct = _load_smtp_account(from_account) if from_account else None
    smtp_host = (acct or {}).get("host", os.environ.get("DELIMIT_SMTP_HOST", "smtp.gmail.com"))
    smtp_port = int((acct or {}).get("port", os.environ.get("DELIMIT_SMTP_PORT", "587")))
    smtp_user = (acct or {}).get("user", os.environ.get("DELIMIT_SMTP_USER", ""))
    smtp_pass = (acct or {}).get("pass", os.environ.get("DELIMIT_SMTP_PASS", ""))
    smtp_from = (acct or {}).get("from", smtp_user)

    if not smtp_pass:
        return False

    content_type = "html" if body.strip().startswith("<html") else "plain"
    msg = MIMEText(body, content_type, "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to], msg.as_string())
        return True
    except Exception as e:
        logger.error("SMTP send failed: %s", e)
        return False


def _render_html_email(subject: str, body: str, event_type: str) -> str:
    """Render a professional HTML email from a plain-text body.

    Converts markdown-like patterns to HTML:
    - Lines starting with "---" become <hr>
    - Lines with ALL CAPS become section headers
    - Lines starting with "- " become list items
    - URLs become clickable links
    - Draft text in quotes gets styled as blockquotes
    - "approve/reject" instructions get styled as action buttons
    """
    import re
    import html as _html

    # Determine accent color from event type
    color_map = {
        "social_draft": "#7C3AED",   # purple — approval needed
        "outreach": "#7C3AED",
        "deploy": "#059669",          # green — deploy/success
        "gate_failure": "#DC2626",    # red — failure/alert
        "digest": "#2563EB",          # blue — informational
        "info": "#2563EB",
    }
    accent = color_map.get(event_type, "#7C3AED")

    # Parse subject for badge
    badge = ""
    badge_match = re.match(r'\[([A-Z]+)\]', subject)
    if badge_match:
        badge = badge_match.group(1)

    def _render_copy_block(label: str, text: str) -> str:
        escaped_text = _html.escape(text.strip("\n"))
        escaped_label = _html.escape(label)
        return (
            f'<div style="margin:14px 0">'
            f'<div style="background:{accent};color:white;padding:8px 12px;'
            f'border-radius:8px 8px 0 0;font-size:12px;font-weight:700;letter-spacing:0.3px">'
            f'{escaped_label}</div>'
            f'<div style="border:1px solid #D1D5DB;border-top:none;border-radius:0 0 8px 8px;'
            f'background:#F9FAFB;padding:12px">'
            f'<div style="font-size:11px;color:#6B7280;margin-bottom:8px">'
            f'Tap and hold inside this block to copy.</div>'
            f'<pre style="margin:0;white-space:pre-wrap;word-break:break-word;'
            f'font:13px/1.55 SFMono-Regular,Consolas,Monaco,monospace;color:#111827">'
            f'{escaped_text}</pre>'
            f'</div>'
            f'</div>'
        )

    # Convert body lines to HTML
    lines = body.split("\n")
    html_lines = []
    in_list = False
    active_copy_label = None
    active_copy_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("--- COPY BELOW THIS LINE ---"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            active_copy_label = "Manual Post Text"
            active_copy_lines = []
            continue

        if stripped.startswith("--- TITLE (paste in title field) ---"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            active_copy_label = "Post Title"
            active_copy_lines = []
            continue

        if stripped.startswith("--- BODY (paste in body field) ---"):
            if active_copy_label and active_copy_lines:
                html_lines.append(_render_copy_block(active_copy_label, "\n".join(active_copy_lines)))
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            active_copy_label = "Post Body"
            active_copy_lines = []
            continue

        if stripped.startswith("--- SOURCE POST TITLE ---"):
            if active_copy_label and active_copy_lines:
                html_lines.append(_render_copy_block(active_copy_label, "\n".join(active_copy_lines)))
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            active_copy_label = "Source Post Title"
            active_copy_lines = []
            continue

        if stripped.startswith("--- SOURCE POST BODY ---"):
            if active_copy_label and active_copy_lines:
                html_lines.append(_render_copy_block(active_copy_label, "\n".join(active_copy_lines)))
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            active_copy_label = "Source Post Body"
            active_copy_lines = []
            continue

        if stripped in ("--- END ---", "--- END COPY ---") and active_copy_label is not None:
            html_lines.append(_render_copy_block(active_copy_label, "\n".join(active_copy_lines)))
            active_copy_label = None
            active_copy_lines = []
            continue

        if active_copy_label is not None:
            active_copy_lines.append(line)
            continue

        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br>")
            continue

        # Draft block headers: "--- Draft <id> (platform) ---"
        if stripped.startswith("--- Draft ") and stripped.endswith("---"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            draft_label = _html.escape(stripped.strip("- ").strip())
            html_lines.append(
                f'<div style="background:{accent};color:white;padding:8px 14px;'
                f'border-radius:6px 6px 0 0;margin-top:16px;font-size:13px;font-weight:700">'
                f'{draft_label}</div>'
                f'<div style="background:#F9FAFB;border:1px solid #E5E7EB;border-top:none;'
                f'border-radius:0 0 6px 6px;padding:12px 14px;margin-bottom:4px">'
            )
            # The next lines until the next "---" or "To approve" will be inside this box
            # We'll close it when we hit the next separator
            continue

        # Horizontal rules / separators
        if stripped.startswith("---") or stripped.startswith("==="):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            # Close any open draft box
            html_lines.append('</div>')
            html_lines.append(f'<hr style="border:none;border-top:1px solid #E5E7EB;margin:16px 0">')
            continue

        # Section headers (ALL CAPS lines or lines ending with colon that are short)
        if (stripped.isupper() and len(stripped) > 3 and len(stripped) < 60) or \
           (stripped.endswith(":") and len(stripped) < 50 and stripped[:-1].replace(" ", "").replace("-", "").isalpha()):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h3 style="color:{accent};font-size:13px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:0.5px;margin:20px 0 8px 0;'
                f'border-bottom:2px solid {accent};padding-bottom:4px">'
                f'{_html.escape(stripped)}</h3>'
            )
            continue

        # List items
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append('<ul style="margin:4px 0;padding-left:20px">')
                in_list = True
            item = _html.escape(stripped[2:])
            # Bold draft IDs
            item = re.sub(r'([0-9a-f]{12})', r'<code style="background:#F3F4F6;padding:1px 4px;border-radius:3px;font-size:12px">\1</code>', item)
            html_lines.append(f'<li style="margin:4px 0;color:#374151">{item}</li>')
            continue

        if in_list:
            html_lines.append("</ul>")
            in_list = False

        escaped = _html.escape(stripped)

        # Convert URLs to clickable links
        escaped = re.sub(
            r'(https?://[^\s<>&"]+)',
            r'<a href="\1" style="color:{};text-decoration:underline">\1</a>'.format(accent),
            escaped,
        )

        # Style quoted draft text
        if escaped.startswith('"') and escaped.endswith('"'):
            html_lines.append(
                f'<blockquote style="border-left:3px solid {accent};margin:8px 0;'
                f'padding:8px 12px;background:#F9FAFB;color:#374151;font-style:italic">'
                f'{escaped}</blockquote>'
            )
            continue

        # Style approve/reject instructions as action callouts
        if any(kw in stripped.lower() for kw in ("to approve", "reply with", "reply \"approve")):
            html_lines.append(
                f'<div style="background:#F0FDF4;border:1px solid #BBF7D0;border-radius:6px;'
                f'padding:10px 14px;margin:12px 0;font-weight:600;color:#166534">'
                f'{escaped}</div>'
            )
            continue

        if any(kw in stripped.lower() for kw in ("to reject", "reply \"reject")):
            html_lines.append(
                f'<div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:6px;'
                f'padding:10px 14px;margin:12px 0;color:#991B1B">'
                f'{escaped}</div>'
            )
            continue

        # Protocol warnings
        if "PROTOCOL WARNING" in stripped:
            html_lines.append(
                f'<div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:6px;'
                f'padding:10px 14px;margin:12px 0;color:#92400E;font-size:12px">'
                f'{escaped}</div>'
            )
            continue

        # Regular paragraph
        html_lines.append(f'<p style="margin:6px 0;color:#374151;line-height:1.5">{escaped}</p>')

    if active_copy_label and active_copy_lines:
        html_lines.append(_render_copy_block(active_copy_label, "\n".join(active_copy_lines)))
    if in_list:
        html_lines.append("</ul>")

    body_html = "\n".join(html_lines)

    # Build full HTML email
    badge_html = ""
    if badge:
        badge_colors = {
            "APPROVE": ("#7C3AED", "#EDE9FE"),
            "ACTION": ("#D97706", "#FEF3C7"),
            "ALERT": ("#DC2626", "#FEE2E2"),
            "URGENT": ("#DC2626", "#FEE2E2"),
            "GATE": ("#DC2626", "#FEE2E2"),
            "DIGEST": ("#2563EB", "#DBEAFE"),
            "INFO": ("#6B7280", "#F3F4F6"),
            "OUTREACH": ("#7C3AED", "#EDE9FE"),
            "DEPLOY": ("#059669", "#D1FAE5"),
        }
        fg, bg = badge_colors.get(badge, ("#6B7280", "#F3F4F6"))
        badge_html = (
            f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;'
            f'letter-spacing:0.5px;margin-bottom:8px">{badge}</span><br>'
        )

    # Clean subject for display (remove bracket prefix)
    display_subject = re.sub(r'^\[[A-Z]+\]\s*', '', subject)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F9FAFB;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;padding:20px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);overflow:hidden">

<!-- Header bar -->
<tr><td style="background:{accent};padding:16px 24px">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td><span style="color:white;font-size:14px;font-weight:700;letter-spacing:0.5px">DELIMIT</span></td>
    <td align="right"><span style="color:rgba(255,255,255,0.8);font-size:11px">{event_type or 'notification'}</span></td>
  </tr></table>
</td></tr>

<!-- Body -->
<tr><td style="padding:24px">
  {badge_html}
  <h2 style="margin:0 0 16px 0;color:#111827;font-size:18px;font-weight:600;line-height:1.3">{_html.escape(display_subject)}</h2>
  {body_html}
</td></tr>

<!-- Footer -->
<tr><td style="background:#F9FAFB;padding:12px 24px;border-top:1px solid #E5E7EB">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td><span style="color:#9CA3AF;font-size:11px">Sent by Delimit governance layer</span></td>
    <td align="right"><a href="https://delimit.ai" style="color:#9CA3AF;font-size:11px;text-decoration:none">delimit.ai</a></td>
  </tr></table>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def send_email(
    to: str = "",
    subject: str = "",
    body: str = "",
    from_account: str = "",
    message: str = "",
    event_type: str = "",
    attachments: list = None,
) -> Dict[str, Any]:
    """Send an email notification via SMTP.

    Args:
        to: Recipient email address. Falls back to DELIMIT_SMTP_TO or
            owner@example.com.
        subject: Email subject line.
        body: Email body text (preferred). Falls back to 'message' for
            backward compatibility.
        from_account: Sender account key in ~/.delimit/secrets/smtp-all.json
            (e.g. 'pro@delimit.ai', 'admin@wire.report'). If provided, SMTP
            credentials are loaded from that file instead of env vars.
        message: Email body text (legacy parameter, use 'body' instead).
        event_type: Event category for filtering/logging.
        attachments: List of file paths to attach to the email.

    Credential resolution order:
        1. from_account lookup in ~/.delimit/secrets/smtp-all.json
        2. DELIMIT_SMTP_* environment variables
    """
    # body takes precedence, fall back to message for backward compat
    email_body = body or message

    timestamp = datetime.now(timezone.utc).isoformat()
    event_key = (event_type or "").lower()
    subject_lower = (subject or "").lower()

    # Batch automated scan output — daemon heartbeats, scan summaries
    # NOTE: social_draft sends IMMEDIATELY — those are the actionable emails
    # with copy text + links that the founder needs to post from.
    force_digest = event_key in (
        "daemon_status", "scan_summary", "heartbeat",
        "github_outreach_queued",
        "social_digest", "digest_suppressed",
    )

    # Only these event types send immediately (founder needs to see them now)
    is_urgent = (not force_digest) and any(tag in event_key + subject_lower
                    for tag in ("p0", "urgent", "alert", "critical", "approve",
                                "founder_directive", "gate_failure",
                                "security", "deploy", "action",
                                "completed", "social_draft"))

    global _last_digest_flush
    with _email_throttle_lock:
        now = _time.time()
        # Prune sends older than 1 hour
        _email_send_times[:] = [t for t in _email_send_times if now - t < 3600]

        if force_digest or (not is_urgent and len(_email_send_times) >= _EMAIL_MAX_PER_HOUR):
            # Queue for digest instead of sending immediately
            _email_digest_queue.append({
                "to": to, "subject": subject, "body": email_body,
                "from_account": from_account, "event_type": event_type,
                "timestamp": timestamp,
            })
            # Flush digest when: 3+ items queued, OR interval elapsed with any items
            queue_size = len(_email_digest_queue)
            interval_elapsed = now - _last_digest_flush >= _EMAIL_DIGEST_INTERVAL
            if (queue_size >= 10) or (interval_elapsed and queue_size > 0):
                _flush_email_digest()
                _last_digest_flush = now
            return {
                "channel": "email",
                "delivered": False,
                "queued": True,
                "reason": "Batched for digest." if force_digest else f"Throttled ({len(_email_send_times)}/{_EMAIL_MAX_PER_HOUR} per hour). Batched for digest.",
                "queue_size": len(_email_digest_queue),
                "timestamp": timestamp,
            }
        _email_send_times.append(now)

    # Try from_account first, then _defaults, then fall back to env vars
    if not from_account:
        defaults = _load_smtp_account("_defaults")
        if defaults and defaults.get("from_account"):
            from_account = defaults["from_account"]
    account_creds = _load_smtp_account(from_account) if from_account else None

    if account_creds:
        smtp_host = account_creds.get("host", "")
        smtp_port = int(account_creds.get("port", 587))
        smtp_user = account_creds.get("user", "")
        smtp_pass = account_creds.get("pass", "")
        smtp_from = account_creds.get("from_alias", from_account)
    else:
        smtp_host = os.environ.get("DELIMIT_SMTP_HOST", "")
        smtp_port = int(os.environ.get("DELIMIT_SMTP_PORT", "587"))
        smtp_user = os.environ.get("DELIMIT_SMTP_USER", "")
        smtp_pass = os.environ.get("DELIMIT_SMTP_PASS", "")
        smtp_from = os.environ.get("DELIMIT_SMTP_FROM", "")

    # Resolve recipient: explicit > env var > smtp-all.json _defaults
    smtp_to = to or os.environ.get("DELIMIT_SMTP_TO", "")
    if not smtp_to:
        defaults = _load_smtp_account("_defaults")
        if defaults:
            smtp_to = defaults.get("to", "")

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
    html_body = _render_html_email(subj, email_body, event_type)

    if attachments:
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        msg = MIMEMultipart()
        msg.attach(MIMEText(html_body, "html"))
        for fpath in attachments:
            fp = Path(fpath)
            if fp.exists():
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fp.read_bytes())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={fp.name}")
                msg.attach(part)
    else:
        msg = MIMEText(html_body, "html")
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


# ═════════════════════════════════════════════════════════════════════
#  Email Protocol — enforced server-side, model-agnostic
# ═════════════════════════════════════════════════════════════════════
# Every email must be self-contained and actionable on mobile.
# The protocol validates required sections per event_type and rejects
# or fixes emails that don't meet the standard.

# Subject line MUST start with one of these brackets:
_VALID_SUBJECT_PREFIXES = (
    "[APPROVE]", "[ACTION]", "[INFO]", "[ALERT]", "[DIGEST]",
    "[URGENT]", "[OUTREACH]", "[DEPLOY]", "[GATE]",
)

# Required sections per event_type. Each is a (header, description) tuple.
_EMAIL_PROTOCOL: Dict[str, List[tuple]] = {
    "social_draft": [
        ("THREAD CONTEXT", "subreddit/platform, post topic, engagement stats"),
        ("DRAFT", "the full draft text, not just an ID"),
        ("TO APPROVE", "reply instructions with draft_id"),
    ],
    "outreach": [
        ("TARGETS FOUND", "list with platform, title/snippet, URL"),
        ("DRAFTS", "full draft text for each, with draft_id"),
        ("TO APPROVE", "reply instructions"),
    ],
    "deploy": [
        ("WHAT CHANGED", "summary of changes being deployed"),
        ("GATES PASSED", "test, security, lint results"),
        ("TO APPROVE", "reply instructions or auto-proceed note"),
    ],
    "gate_failure": [
        ("WHAT FAILED", "which gate and why"),
        ("IMPACT", "what is blocked"),
        ("TO FIX", "next steps"),
    ],
    "digest": [
        ("COMPLETED", "what was done since last digest"),
        ("PENDING YOUR ACTION", "items needing founder response"),
    ],
}


def _load_drafts_by_ids(draft_ids: list) -> list:
    """Load draft entries from social_drafts.jsonl matching the given IDs."""
    drafts_file = Path.home() / ".delimit" / "social_drafts.jsonl"
    if not drafts_file.exists():
        return []
    results = []
    id_set = set(draft_ids)
    try:
        for line in drafts_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                if d.get("draft_id") in id_set and d.get("status") == "pending":
                    results.append(d)
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        pass
    return results


def _enforce_email_protocol(subject: str, message: str, event_type: str) -> tuple:
    """Validate and fix email against the protocol. Returns (subject, message, warnings)."""
    warnings = []

    # 1. Subject must have a valid prefix bracket
    if not any(subject.startswith(p) for p in _VALID_SUBJECT_PREFIXES):
        # Try to infer from event_type
        # LED-969: customer-facing emails should not get bracket prefixes.
        # Any event_type starting with "customer_" is external-facing and
        # the subject should be sent as-is (clean, professional).
        if event_type and event_type.startswith("customer_"):
            pass  # no prefix for customer emails
        else:
            prefix_map = {
                "social_draft": "[APPROVE]",
                "outreach": "[OUTREACH]",
                "deploy": "[DEPLOY]",
                "gate_failure": "[ALERT]",
                "digest": "[DIGEST]",
                "info": "[INFO]",
            }
            prefix = prefix_map.get(event_type, "[INFO]")
            subject = f"{prefix} {subject}"
            warnings.append(f"Subject prefix added: {prefix}")

    # 2. Check required sections for this event_type
    required = _EMAIL_PROTOCOL.get(event_type, [])
    msg_upper = message.upper()
    missing = []
    for header, desc in required:
        # Check if the section header appears (case-insensitive, with or without colon)
        if header.upper() not in msg_upper:
            missing.append(f"{header} ({desc})")

    if missing:
        # Append a protocol warning to the email body so the founder sees what's missing
        message += "\n\n" + "=" * 40
        message += "\nPROTOCOL WARNING — Missing required sections:"
        for m in missing:
            message += f"\n  - {m}"
        message += "\n\nThis email may not be fully actionable. The sending model"
        message += "\nskipped required context. Check drafts via delimit_social_approve."
        warnings.append(f"Missing sections: {', '.join(m.split(' (')[0] for m in missing)}")

    # 3. Outreach/social_draft emails — auto-inject full draft text from social_drafts.jsonl
    if event_type in ("social_draft", "outreach"):
        import re
        draft_ids = re.findall(r'[0-9a-f]{12}', message)
        if draft_ids:
            drafts = _load_drafts_by_ids(draft_ids)
            if drafts:
                message += "\n\n" + "=" * 40
                message += "\nCOPY-READY DRAFTS"
                message += "\n" + "=" * 40
                for d in drafts:
                    did = d.get("draft_id", "")
                    text = d.get("text", "")
                    platform = d.get("platform", "")
                    ctx = d.get("context", "")
                    thread_url = d.get("thread_url", "")
                    reply_to_id = d.get("reply_to_id", "")
                    # Try to extract URL from context if thread_url is empty
                    if not thread_url and ctx:
                        url_match = re.search(r'https?://[^\s]+', ctx)
                        if url_match:
                            thread_url = url_match.group(0)
                    message += f"\n\n--- Draft {did} ({platform}) ---"
                    message += f"\nWHERE: {platform}"
                    where_link = thread_url or (f"https://x.com/i/status/{reply_to_id}" if reply_to_id else "")
                    if where_link:
                        message += f"\nLINK: {where_link}"
                    if ctx:
                        message += f"\nWHY: {ctx}"
                    message += f"\nWHAT:\n--- COPY BELOW THIS LINE ---\n{text}\n--- END COPY ---"
                    message += f"\n\nTo approve: reply \"approve {did}\""
                message += "\n\n" + "=" * 40
                warnings.append(f"Auto-injected {len(drafts)} draft texts from social_drafts.jsonl")

    # 4. Always append the standard footer
    if "delimit.ai" not in message.lower() and "Delimit" not in message:
        message += "\n\n---\nSent by Delimit governance layer"

    return subject, message, warnings


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

    # Enforce email protocol for all email notifications
    protocol_warnings = []
    if channel == "email":
        subject, message, protocol_warnings = _enforce_email_protocol(subject, message, event_type)

    if channel == "webhook":
        return send_webhook(webhook_url, message, event_type)
    elif channel == "slack":
        return send_slack(webhook_url, message, event_type)
    elif channel == "email":
        result = send_email(
            to=to,
            subject=subject,
            message=message,
            from_account=from_account,
            event_type=event_type,
        )
        if protocol_warnings:
            result["protocol_warnings"] = protocol_warnings
        return result
    elif channel == "telegram":
        return send_telegram(message=message, event_type=event_type)
    else:
        return {"error": f"Unknown channel: {channel}. Supported: webhook, slack, email, telegram"}


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
    if not smtp_pass and IMAP_USER:
        account = _load_smtp_account(IMAP_USER)
        smtp_pass = str((account or {}).get("pass") or (account or {}).get("password") or "")
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

        # Dedup: track processed Message-IDs to avoid triple-forwarding
        _processed_cache_path = Path(os.path.expanduser("~/.delimit/inbox_processed.json"))
        _processed_ids: set = set()
        try:
            if _processed_cache_path.exists():
                _processed_ids = set(json.loads(_processed_cache_path.read_text()))
        except Exception:
            _processed_ids = set()

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

            # Dedup: skip if we already processed this Message-ID
            message_id_header = msg.get("Message-ID", msg_id.decode())
            if message_id_header in _processed_ids:
                imap.store(msg_id, "+FLAGS", "\\Seen")
                continue

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
            _processed_ids.add(message_id_header)
            _record_inbox_routing({**entry, "timestamp": timestamp, "process_mode": process})

        # Persist processed IDs (keep last 500 to avoid unbounded growth)
        try:
            trimmed = list(_processed_ids)[-500:]
            _processed_cache_path.parent.mkdir(parents=True, exist_ok=True)
            _processed_cache_path.write_text(json.dumps(trimmed))
        except Exception:
            pass

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
