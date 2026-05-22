"""LED-1264 daily digest assembler.

Reads ``~/.delimit/scan_bridge_promotions.jsonl`` and assembles ONE
email-ready digest of the last 24h of promotions. Returns ``None``
(or empty subject/body) on a zero-signal day so the caller can skip
sending — silent days are fine per the directive.

The digest text is intentionally plain — no markdown, no html — so
the same string can be used as an email body or a Slack message
without re-formatting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROMOTIONS_LOG = Path.home() / ".delimit" / "scan_bridge_promotions.jsonl"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_promotions(log_path: Path) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return out


def _filter_window(
    promotions: List[Dict[str, Any]], since: datetime
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in promotions:
        ts = _parse_iso(p.get("ts"))
        if ts and ts >= since:
            out.append(p)
    return out


def build_daily_digest(
    *,
    now: Optional[datetime] = None,
    window_hours: int = 24,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Assemble the last-N-hour promotion digest.

    Returns
    -------
    dict::

        {
          "subject": "Delimit scan-bridge — N strategic items (last 24h)",
          "body":    "<plain text body>",
          "count":   N,
          "since":   ISO datetime,
          "items":   [...promotion rows...],
        }

    When ``count == 0`` the subject and body are empty strings so the
    caller can short-circuit ("no email on silent days") without having
    to re-check ``count``.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    log_path = log_path or PROMOTIONS_LOG

    promotions = _load_promotions(log_path)
    items = _filter_window(promotions, since)
    items.sort(key=lambda p: p.get("ts") or "", reverse=True)

    if not items:
        return {
            "subject": "",
            "body": "",
            "count": 0,
            "since": since.isoformat(),
            "items": [],
        }

    lines: List[str] = []
    lines.append(
        f"Delimit scan-bridge auto-promoted {len(items)} strategic signal(s) "
        f"to the strategy ledger in the last {window_hours}h."
    )
    lines.append("")
    lines.append(
        "All items are P2 (review, not auto-action). Reply with item id + "
        "decision (escalate, archive, defer) or open the ledger to triage."
    )
    lines.append("")
    lines.append("─" * 70)
    for p in items:
        title = p.get("title") or "(no title)"
        item_id = p.get("item_id") or "(unassigned)"
        confidence = p.get("confidence")
        platform = p.get("platform") or "?"
        url = p.get("canonical_url") or ""
        first_seen = p.get("first_seen") or ""
        try:
            conf_str = f"{float(confidence):.2f}" if confidence is not None else "?"
        except (TypeError, ValueError):
            conf_str = str(confidence)
        lines.append(f"[{item_id}] {title}")
        lines.append(f"    platform={platform} confidence={conf_str} first_seen={first_seen}")
        if url:
            lines.append(f"    {url}")
        lines.append("")

    lines.append("─" * 70)
    lines.append("")
    lines.append(
        "Source: ~/.delimit/scan_bridge_promotions.jsonl. "
        "Skipped duplicates: ~/.delimit/scan_bridge_dedup.jsonl. "
        "Tune via DELIMIT_SCAN_PROMO_CONFIDENCE (default 0.85)."
    )

    body = "\n".join(lines)
    subject = f"Delimit scan-bridge — {len(items)} strategic item(s) (last {window_hours}h)"
    return {
        "subject": subject,
        "body": body,
        "count": len(items),
        "since": since.isoformat(),
        "items": items,
    }
