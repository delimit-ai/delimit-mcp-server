"""LED-1264: scan → strategy-ledger auto-promote bridge.

Pure consumer of ``~/.delimit/social_targets.jsonl`` (the existing
``delimit_social_target`` output). Promotes a tightly-gated subset of
strategic signals into the strategy ledger so the founder reviews them
via a daily digest instead of inbox-spam pings.

Panel decision (UNANIMOUS R3, 2026-05-07): tight guards
(strategic + confidence ≥ 0.85 + dedup against open / 60-day-closed),
P2 priority (review, not auto-action), one daily digest email.

Public entry points:

- :func:`bridge.promote_recent_signals` — main work function
- :func:`digest.build_daily_digest` — assemble last-24h digest text
- :func:`bridge.backfill_from` — one-time idempotent backfill walker

The bridge is invoked by ``scripts/scan_bridge_cron.py`` on a 6-hour
crontab cadence (founder applies manually). Direct in-process calls to
``ai.ledger_manager.add_item`` — no MCP subprocess.
"""

from ai.scan_bridge.bridge import (
    backfill_from,
    promote_recent_signals,
)
from ai.scan_bridge.dedup import (
    extract_topic_fingerprint,
    is_duplicate,
)
from ai.scan_bridge.digest import build_daily_digest

__all__ = [
    "backfill_from",
    "build_daily_digest",
    "extract_topic_fingerprint",
    "is_duplicate",
    "promote_recent_signals",
]
