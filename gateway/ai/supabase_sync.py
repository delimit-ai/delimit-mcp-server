"""Supabase sync -- writes gateway data to cloud for dashboard access.

Writes are fire-and-forget (never blocks tool execution).
If Supabase is unreachable, data stays in local files (always the source of truth).
"""
import json
import os
import logging
import uuid
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("delimit.supabase_sync")

_client = None
_init_attempted = False
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Also check local secrets file
if not SUPABASE_URL:
    secrets_file = Path.home() / ".delimit" / "secrets" / "supabase.json"
    if secrets_file.exists():
        try:
            creds = json.loads(secrets_file.read_text())
            SUPABASE_URL = creds.get("url", "")
            SUPABASE_KEY = creds.get("service_role_key", "")
        except Exception:
            pass


_VENTURE_CANONICAL = {
    "delimit": "delimit",
    "domainvested": "domainvested",
    "domain_vested": "domainvested",
    "dv": "domainvested",
    "wirereport": "wirereport",
    "wire_report": "wirereport",
    "wire.report": "wirereport",
    "wr": "wirereport",
    "livetube": "livetube",
    "livetubeai": "livetube",
    "livetube.ai": "livetube",
    "lt": "livetube",
}


def _normalize_venture(value) -> str:
    """LED-1008: map freeform venture strings into the canonical 4-member
    vocabulary the Inbox + Ventures surfaces expect. Blank stays blank
    (unattributed). Unknown values pass through lowercased so we notice
    them in the dashboard instead of silently losing them."""
    if not value:
        return ""
    key = str(value).strip().lower()
    return _VENTURE_CANONICAL.get(key, key)


def _get_client():
    """Lazy-init Supabase client. Returns the SDK client, 'http' for fallback, or None."""
    global _client, _init_attempted
    if _client is not None:
        return _client
    if _init_attempted:
        return _client  # Already tried and failed, return cached result (may be None or "http")
    _init_attempted = True
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _client
    except ImportError:
        logger.debug("supabase-py not installed, using HTTP fallback")
        _client = "http"
        return _client
    except Exception as e:
        logger.warning(f"Supabase init failed: {e}")
        _client = "http"  # Fall back to HTTP rather than giving up entirely
        return _client


def _http_post(table: str, data: dict, headers_extra: Optional[Dict] = None) -> bool:
    """POST to Supabase REST API without the SDK."""
    import urllib.request
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Prefer", "return=minimal")
        if headers_extra:
            for k, v in headers_extra.items():
                req.add_header(k, v)
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.debug(f"Supabase HTTP POST to {table} failed: {e}")
        return False


def _http_patch(table: str, query: str, data: dict) -> bool:
    """PATCH to Supabase REST API without the SDK."""
    import urllib.request
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method="PATCH")
        req.add_header("Content-Type", "application/json")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Prefer", "return=minimal")
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.debug(f"Supabase HTTP PATCH to {table} failed: {e}")
        return False


def sync_event(event: dict):
    """Sync an event to Supabase (fire-and-forget).

    Maps the gateway event dict to the Supabase events table schema:
      id (uuid, required), type (text, required), tool (text, required),
      ts, model, status, venture, detail, user_id, session_id
    """
    try:
        client = _get_client()
        if client is None:
            return
        row = {
            "id": str(uuid.uuid4()),
            "type": event.get("type", "tool_call"),
            "tool": event.get("tool", "unknown"),
            "ts": event.get("ts", ""),
            "model": event.get("model", ""),
            "status": event.get("status", "ok"),
            "venture": event.get("venture", ""),
            "session_id": event.get("session_id", ""),
            "user_id": event.get("user_id", ""),
        }
        # Include risk_level and trace info in detail field
        detail_parts = []
        if event.get("risk_level"):
            detail_parts.append(f"risk={event['risk_level']}")
        if event.get("trace_id"):
            detail_parts.append(f"trace={event['trace_id']}")
        if event.get("span_id"):
            detail_parts.append(f"span={event['span_id']}")
        if detail_parts:
            row["detail"] = " ".join(detail_parts)

        if client == "http":
            _http_post("events", row)
        else:
            client.table("events").insert(row).execute()
    except Exception as e:
        logger.debug(f"Event sync failed: {e}")


def sync_ledger_item(item: dict):
    """Sync a ledger item to Supabase (upsert).

    Maps the gateway ledger item to the Supabase ledger_items table schema:
      id (text, required), title (text, required), priority, venture,
      status, description, source, note, assignee
    """
    try:
        client = _get_client()
        if client is None:
            return
        row = {
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "priority": item.get("priority", "P1"),
            "venture": item.get("venture", ""),
            "status": item.get("status", "open"),
            "description": item.get("description", ""),
            "source": item.get("source", "mcp"),
        }
        if not row["id"] or not row["title"]:
            return  # Required fields missing
        if client == "http":
            _http_post(
                "ledger_items",
                row,
                headers_extra={
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
        else:
            client.table("ledger_items").upsert(row).execute()
    except Exception as e:
        logger.debug(f"Ledger item sync failed: {e}")


def sync_ledger_update(item_id: str, status: str, note: str = ""):
    """Sync a ledger status update to Supabase."""
    try:
        client = _get_client()
        if client is None:
            return
        update = {"status": status}
        if note:
            update["note"] = note
        if status == "done":
            from datetime import datetime, timezone
            update["completed_at"] = datetime.now(timezone.utc).isoformat()

        if client == "http":
            _http_patch("ledger_items", f"id=eq.{item_id}", update)
        else:
            client.table("ledger_items").update(update).eq("id", item_id).execute()
    except Exception as e:
        logger.debug(f"Ledger update sync failed: {e}")


def sync_work_order(wo: dict):
    """Sync a work order to Supabase for dashboard inbox.

    Gateway-local work order (from ai.work_order.create_work_order) maps to
    the Supabase work_orders table (migration 020).
    """
    try:
        client = _get_client()
        if client is None:
            return
        row = {
            "id": wo.get("id", ""),
            "title": wo.get("title", ""),
            "goal": wo.get("goal", ""),
            "context": wo.get("context", ""),
            "steps": wo.get("steps", []),
            "acceptance_criteria": wo.get("acceptance_criteria", []),
            "ledger_item_id": wo.get("ledger_item_id", ""),
            "priority": wo.get("priority", "P1"),
            "tools_needed": wo.get("tools_needed", []),
            "estimated_minutes": wo.get("estimated_minutes", 15),
            "worker_type": wo.get("worker_type", ""),
            "status": wo.get("status", "pending"),
            "preview": wo.get("preview", "")[:2000],
            "artifact_path": wo.get("filepath", ""),
            "executable_actions": wo.get("executable_actions", []),
            "execution_status": wo.get("execution_status", ""),
            "execution_log": wo.get("execution_log", []),
            "executed_at": wo.get("executed_at"),
            "executed_by": wo.get("executed_by", ""),
            "venture": _normalize_venture(wo.get("venture", "")),
        }
        if not row["id"] or not row["title"]:
            return
        if client == "http":
            _http_post(
                "work_orders",
                row,
                headers_extra={
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
        else:
            client.table("work_orders").upsert(row).execute()

        # LED-977 (rescoped): push notification on new pending WO. Safe to
        # call every time sync runs; notify_new_work_order dedupes via the
        # sent-marker file so only the initial pending insert pushes.
        try:
            notify_new_work_order(row)
        except Exception as exc:
            logger.debug("notify_new_work_order hook failed: %s", exc)
    except Exception as e:
        logger.debug(f"Work order sync failed: {e}")


def sync_deliberation(delib: dict):
    """Sync a deliberation transcript summary to Supabase."""
    try:
        client = _get_client()
        if client is None:
            return
        row = {
            "id": delib.get("id") or delib.get("transcript_saved", "").split("/")[-1].replace(".json", ""),
            "question": delib.get("question", "")[:2000],
            "context": delib.get("context", "")[:2000],
            "scope": delib.get("scope", ""),
            "models_participated": delib.get("models_participated", []),
            "rounds": delib.get("rounds", 0),
            "status": delib.get("status", "unknown"),
            "final_verdict": delib.get("final_verdict", "")[:4000],
            "transcript_path": delib.get("transcript_saved", ""),
            "ledger_items_created": delib.get("ledger_items_created", []),
            "venture": _normalize_venture(delib.get("venture", "")),
        }
        if not row["id"] or not row["question"]:
            return
        if client == "http":
            _http_post(
                "deliberations",
                row,
                headers_extra={
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
        else:
            client.table("deliberations").upsert(row).execute()
    except Exception as e:
        logger.debug(f"Deliberation sync failed: {e}")


# ---------------------------------------------------------------------------
# LED-977 (rescoped): push notifications on new work-order approvals.
# Uses ntfy.sh — free, open-source, zero account/API-key management. User
# installs the ntfy mobile app (iOS/Android), subscribes to their private
# topic URL, and taps the push to deep-link into the dashboard inbox.
# ---------------------------------------------------------------------------

NTFY_ENV_KEY = "DELIMIT_NTFY_TOPIC"
NTFY_BASE_URL = os.environ.get("DELIMIT_NTFY_BASE_URL", "https://ntfy.sh")
NTFY_CLICK_URL = os.environ.get(
    "DELIMIT_NTFY_CLICK_URL",
    "https://app.delimit.ai/dashboard/inbox",
)
# Cache so we only send one push per WO — a WO gets upserted every time its
# status changes (approved, executed, etc), but the push is only meaningful
# on the initial pending insert.
_NTFY_SENT_FILE = Path.home() / ".delimit" / "notifications" / "ntfy_sent.jsonl"


def _ntfy_already_sent(wo_id: str) -> bool:
    """Check if we've already pushed a notification for this WO."""
    if not _NTFY_SENT_FILE.exists():
        return False
    try:
        with _NTFY_SENT_FILE.open() as fh:
            for line in fh:
                try:
                    if json.loads(line).get("wo_id") == wo_id:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _ntfy_record_sent(wo_id: str, title: str) -> None:
    """Append a sent marker so the next sync for the same WO is a no-op."""
    try:
        _NTFY_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _NTFY_SENT_FILE.open("a") as fh:
            fh.write(json.dumps({
                "wo_id": wo_id,
                "title": title[:80],
                "ts": os.environ.get("_NTFY_TS_OVERRIDE") or __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ"),
            }) + "\n")
    except Exception as exc:
        logger.debug("ntfy sent-marker write failed: %s", exc)


def notify_new_work_order(wo: dict) -> None:
    """Fire a push when a brand-new pending work order lands.

    Fire-and-forget. Silent no-op when DELIMIT_NTFY_TOPIC isn't set so this
    is opt-in — the service keeps working without it.
    """
    topic = os.environ.get(NTFY_ENV_KEY, "").strip()
    if not topic:
        return
    if wo.get("status") != "pending":
        return
    wo_id = wo.get("id", "")
    if not wo_id or _ntfy_already_sent(wo_id):
        return

    title = wo.get("title") or "New work order"
    priority = str(wo.get("priority") or "P2").upper()
    worker = wo.get("worker_type") or "worker"
    body_lines = [wo.get("goal", "")[:200]]
    if wo.get("ledger_item_id"):
        body_lines.append(f"Source: {wo['ledger_item_id']}")
    body_lines.append(f"Drafted by: {worker}")
    body = "\n".join(l for l in body_lines if l).strip()

    try:
        import urllib.request
        url = f"{NTFY_BASE_URL.rstrip('/')}/{topic}"
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title": f"[Delimit {priority}] {title}"[:180],
                "Tags": "memo" if priority == "P2" else "warning",
                "Click": NTFY_CLICK_URL,
                "Priority": {"P0": "5", "P1": "4", "P2": "3"}.get(priority, "3"),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                _ntfy_record_sent(wo_id, title)
                logger.info("ntfy push sent wo=%s", wo_id)
            else:
                logger.warning("ntfy push unexpected status: %s", resp.status)
    except Exception as exc:
        logger.warning("ntfy push failed for %s: %s", wo_id, exc)


def sync_social_draft(draft: dict):
    """Sync a social draft row to Supabase + optional ntfy push.

    `draft` is the same shape save_draft() writes to social_drafts.jsonl.
    Fire-and-forget like the other sync helpers — an outage here never
    blocks the drafting worker.
    """
    try:
        client = _get_client()
        if client is None:
            return
        row = {
            "draft_id": draft.get("draft_id", ""),
            "platform": draft.get("platform", ""),
            "account": draft.get("account", ""),
            "text": draft.get("text", ""),
            "thread_url": draft.get("thread_url", ""),
            "context": draft.get("context", ""),
            "source_fingerprint": draft.get("source_fingerprint", ""),
            "quality": draft.get("quality", "review"),
            "status": draft.get("status", "pending"),
            "quote_tweet_id": draft.get("quote_tweet_id", ""),
            "reply_to_id": draft.get("reply_to_id", ""),
            "conversion_target": draft.get("conversion_target", ""),
            "notification_message_id": draft.get("notification_message_id", ""),
            "timestamp": draft.get("timestamp"),
            "venture": _normalize_venture(draft.get("venture", "")),
        }
        if not row["draft_id"] or not row["text"]:
            return
        if client == "http":
            _http_post(
                "social_drafts",
                row,
                headers_extra={
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
        else:
            client.table("social_drafts").upsert(row).execute()

        # ntfy on NEW pending drafts only, dedupe via the WO sent-marker file
        # (reused — scoped by draft_id vs wo_id so no collision)
        if row["status"] == "pending":
            try:
                _push_draft_notification(row)
            except Exception as exc:
                logger.debug("draft ntfy failed: %s", exc)
    except Exception as e:
        logger.debug(f"Social draft sync failed: {e}")


def _push_draft_notification(row: dict) -> None:
    """Fire a medium-priority ntfy when a new pending draft lands."""
    import time as _time
    topic = os.environ.get(NTFY_ENV_KEY, "").strip()
    if not topic:
        return
    draft_id = row.get("draft_id", "")
    if not draft_id or _ntfy_already_sent(draft_id):
        return
    platform = (row.get("platform") or "?").upper()
    quality = row.get("quality") or "?"
    title = f"[Delimit DRAFT {platform}/{quality}]"
    body_preview = (row.get("text") or "").replace("\n", " ")[:220]
    body_lines = [
        f"Thread: {row.get('thread_url', '?')}",
        f"Account: {row.get('account', '?')}",
        "",
        body_preview,
    ]
    try:
        import urllib.request as _ur
        req = _ur.Request(
            f"{NTFY_BASE_URL.rstrip('/')}/{topic}",
            data="\n".join(body_lines).encode(),
            headers={
                "Title": title[:180],
                "Tags": "memo" if quality == "ready" else "warning",
                "Click": "https://app.delimit.ai/dashboard/inbox",
                "Priority": "4" if quality == "ready" else "3",
            },
            method="POST",
        )
        with _ur.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                _ntfy_record_sent(draft_id, row.get("text", "")[:80])
                logger.info("draft ntfy sent draft_id=%s", draft_id)
    except Exception as exc:
        logger.warning("draft ntfy push failed for %s: %s", draft_id, exc)
