"""LED-193 cost tracking + circuit breakers.

MVP profiles are deterministic (no LLM): cost is always 0.0. The cost
infrastructure exists ahead of time so when Class C ``bounded_patch``
graduates, the breakers are wired and unit-tested rather than bolted
on under pressure.

Hard caps (panel-locked):
    - Per-item LLM cost: $2  (DELIMIT_LED193_PER_ITEM_USD override)
    - Daily LLM cost:    $10 (DELIMIT_LED193_DAILY_USD override)

Daily window = trailing 24h, summed from the audit log
(``cost_estimate`` field). Per-item is enforced by callers BEFORE
incurring the cost — exceeded → return ``CapTriggered`` and the executor
short-circuits.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ai.led193_daemon.audit import recent_results

DEFAULT_PER_ITEM_USD = 2.00
DEFAULT_DAILY_USD = 10.00


def per_item_cap() -> float:
    raw = os.environ.get("DELIMIT_LED193_PER_ITEM_USD", "")
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_PER_ITEM_USD


def daily_cap() -> float:
    raw = os.environ.get("DELIMIT_LED193_DAILY_USD", "")
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_DAILY_USD


def daily_spend(
    *,
    audit_log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> float:
    """Sum of cost_estimate across audit records in the last 24h."""
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - 86400.0
    total = 0.0
    for rec in recent_results(audit_log_path=audit_log_path, limit=500):
        ts = rec.get("ts") or ""
        try:
            rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if rec_dt.timestamp() < cutoff:
            continue
        try:
            total += float(rec.get("cost_estimate") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def check_caps(
    *,
    estimated_cost: float = 0.0,
    audit_log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Return ``{"ok": bool, "reason": str, ...}``.

    Caller passes ``estimated_cost`` for the proposed item; we check
    BOTH the per-item cap AND the projected daily total. Deterministic
    profiles pass ``estimated_cost=0.0`` and always return ``ok=True``
    unless the daily cap is already breached (which would only happen
    under a misconfigured override).
    """
    per_cap = per_item_cap()
    if estimated_cost > per_cap:
        return {
            "ok": False,
            "reason": f"per_item_cap_exceeded: ${estimated_cost:.2f} > ${per_cap:.2f}",
            "estimated_cost": estimated_cost,
            "per_item_cap": per_cap,
        }
    spent = daily_spend(audit_log_path=audit_log_path, now=now)
    d_cap = daily_cap()
    projected = spent + estimated_cost
    if projected > d_cap:
        return {
            "ok": False,
            "reason": f"daily_cap_exceeded: ${spent:.2f} + ${estimated_cost:.2f} > ${d_cap:.2f}",
            "daily_spend": spent,
            "daily_cap": d_cap,
            "estimated_cost": estimated_cost,
        }
    return {
        "ok": True,
        "reason": "",
        "daily_spend": spent,
        "daily_cap": d_cap,
        "per_item_cap": per_cap,
        "estimated_cost": estimated_cost,
    }


def record_run(actual_cost: float) -> float:
    """Pass-through for callers that want to declare an actual cost.

    The actual cost lands in the audit log via the ``cost_estimate``
    field on the record. This helper exists so executor call-sites read
    consistently. Returns the validated, clamped cost.
    """
    try:
        v = float(actual_cost)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v)
