"""LED-1908/LED-1909 — append-only evidence audit stream.

Evidence events (deliberation consensus records, gate-pass records,
heartbeat records) are RECORDS of things that happened, not actionable work.
Landing them in the WORK ledger as open items polluted the ledger with
non-actionable rows. This module gives them their own append-only stream:

    ~/.delimit/audit_stream.jsonl   — one JSON object per line:
        {ts, kind, source, quorum, provenance, summary, payload}

Design constraints (never-break-installs):
  * ADDITIVE — a brand-new file; no existing storage format (memories,
    ledger, evidence) is touched or migrated here.
  * EXCEPTION-SAFE — ``append_event`` never raises; a failed audit write
    returns {"ok": False, ...} and must never break the calling tool.
  * APPEND-ONLY — the stream is only ever appended to, never rewritten.

Gate FAILURES and sensor findings are actionable and deliberately KEEP
flowing to the work ledger — only evidence-class events route here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.audit_stream")

AUDIT_STREAM_PATH = Path.home() / ".delimit" / "audit_stream.jsonl"

# Cap the serialized payload per line so a giant tool result can never bloat
# the stream. The summary field carries the human-readable gist regardless.
_MAX_PAYLOAD_CHARS = 4000


def _summarize(payload: Any) -> str:
    """A compact human-readable summary of the payload (best-effort)."""
    try:
        if isinstance(payload, dict):
            for key in ("title", "summary", "question", "note", "final_verdict"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:200]
            return ", ".join(sorted(payload.keys()))[:200]
        return str(payload)[:200]
    except Exception:  # noqa: BLE001
        return ""


def append_event(
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    source: str = "",
    quorum: Optional[Dict[str, Any]] = None,
    provenance: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one evidence event to the audit stream. NEVER raises.

    Args:
        kind: event class, e.g. "deliberation_consensus", "gate_pass",
            "heartbeat".
        payload: arbitrary JSON-serializable event detail (size-capped).
        source: emitting surface, e.g. "governance:deliberate".
        quorum: optional LED-1908 quorum record for deliberation events.
        provenance: optional provenance metadata (origin/mandate/etc.).
        path: override target file (tests). Default AUDIT_STREAM_PATH.

    Returns:
        {"ok": True, "path": ...} or {"ok": False, "error": ...}.
    """
    try:
        target = Path(path) if path is not None else AUDIT_STREAM_PATH
        event: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": str(kind or "event"),
            "source": str(source or ""),
            "summary": _summarize(payload),
        }
        if quorum is not None:
            event["quorum"] = quorum
        if provenance is not None:
            event["provenance"] = provenance
        if payload is not None:
            raw = json.dumps(payload, ensure_ascii=False, default=str)
            if len(raw) > _MAX_PAYLOAD_CHARS:
                event["payload_truncated"] = True
                raw = raw[:_MAX_PAYLOAD_CHARS]
                event["payload"] = raw
            else:
                event["payload"] = payload
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        return {"ok": True, "path": str(target), "kind": event["kind"]}
    except Exception as exc:  # noqa: BLE001 — audit write must never break a caller
        logger.warning("audit_stream append failed (%s): %s", kind, exc)
        return {"ok": False, "error": str(exc), "kind": str(kind or "event")}
