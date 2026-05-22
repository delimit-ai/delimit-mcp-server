"""Single-responsibility cron for autonomous github outreach (LED-2214b).

The 2026-05-11 deliberation (transcript stored privately)
unanimously chose a NEW dedicated daemon over (a) extending the
existing social_daemon (different responsibility — inbound sensing
vs outbound engagement) and (b) composing via the generic
``delimit_loop_config`` primitive (insufficient evidence for the
conditional branching the workflow needs). This is that daemon.

It is deliberately a single-tick function with no internal lifecycle
management — the file is imported by an external scheduler (cron,
``loop_daemon``, manual MCP call). Lifecycle concerns (interval,
backoff, retries) live at the scheduler layer. This keeps the daemon
trivial to reason about, easy to roll back, and explicit-by-default
for the upcoming 30-day operating-model review (2026-05-30).

Single tick:

  1. Monitor phase — for every open intel-class outreach LED with a
     resolvable github issue URL, call ``delimit_sensor_github_issue``.
     New comments / state changes are appended to the LED.
  2. Scan phase — invoke the existing github scanner via
     :func:`ai.social_target.scan_targets` and :func:`process_targets`.
     The substantive-dispatch path in ``process_targets`` fires on
     any target that yields a :class:`SubstantiveCandidate`.
  3. Cap — at most ``max_dispatch`` (default 3) new substantive
     dispatches per tick, to bound fan-out (the bulk-29-cancel
     pattern's lesson).
  4. Kill switch — either the env var
     ``DELIMIT_GITHUB_OUTREACH_DISABLED`` set to a truthy value, or
     the sentinel file ``~/.delimit/outreach_pause`` present,
     short-circuits the tick at entry. No partial work, no state
     mutations.

Public surface:

  * :func:`tick` — run one cycle, return a summary dict.
  * :func:`kill_switch_active` — check the kill-switch state.

The MCP-facing wrapper lives at :func:`ai.server.delimit_outreach_loop_tick`.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.outreach_loop_daemon")

KILL_SWITCH_ENV = "DELIMIT_GITHUB_OUTREACH_DISABLED"
KILL_SWITCH_FILE = Path.home() / ".delimit" / "outreach_pause"

DEFAULT_MAX_DISPATCH = 3
DEFAULT_MAX_MONITOR = 50

# Tag set the intel-class outreach LEDs carry. We use this to retrieve
# the universe of items the daemon is responsible for monitoring.
_OUTREACH_INTEL_TAGS = ("intel", "github-scan")

# Issue / PR URL parser. We accept both /issues/N and /pull/N.
_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<repo>[^/]+/[^/]+)/(?:issues|pull)/(?P<num>\d+)"
)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def kill_switch_active() -> Tuple[bool, str]:
    """Return ``(active, reason)``.

    Either the env var or the sentinel file is sufficient to halt the
    tick. Env var wins for cleanliness in containerized environments;
    the sentinel file is the emergency-stop founders or operators can
    touch from any shell without restarting the parent process.
    """
    env = os.environ.get(KILL_SWITCH_ENV, "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True, f"env:{KILL_SWITCH_ENV}={env}"
    if KILL_SWITCH_FILE.exists():
        return True, f"file:{KILL_SWITCH_FILE}"
    return False, ""


# ---------------------------------------------------------------------------
# Monitor phase
# ---------------------------------------------------------------------------


def _parse_issue_url(url: str) -> Optional[Tuple[str, int]]:
    if not url:
        return None
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        return None
    try:
        return m.group("repo"), int(m.group("num"))
    except (TypeError, ValueError):
        return None


def _open_intel_items(venture: str, limit: int) -> List[Dict[str, Any]]:
    """Return open intel-class outreach LEDs for a venture.

    Pulls from ai.ledger_manager.list_items with the same project_path
    resolution social_target uses. Filters defensively in Python (the
    list_items tag filter is "contains all" but we want to combine
    tag + status, and the simpler path is post-filter).
    """
    try:
        from ai.ledger_manager import list_items
        from ai.social_target import _resolve_venture_project_path

        project_path = _resolve_venture_project_path(venture)
    except Exception as exc:
        logger.warning("monitor_phase: resolver import failed: %s", exc)
        return []

    items: List[Dict[str, Any]] = []
    try:
        # Pull both ledgers — intel items may land on ``strategy`` or
        # ``ops`` depending on category.
        for ledger in ("strategy", "ops"):
            page = list_items(
                ledger=ledger,
                project_path=project_path,
                limit=max(limit, 1),
            )
            for it in page.get("items", {}).get(ledger, []):
                tags = set(it.get("tags") or [])
                if it.get("status") not in {"open", "in_progress"}:
                    continue
                if not all(t in tags for t in _OUTREACH_INTEL_TAGS):
                    continue
                items.append(it)
    except Exception as exc:
        logger.warning("monitor_phase: list_items failed: %s", exc)
        return []
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return items[:limit]


def _extract_issue_url(item: Dict[str, Any]) -> Optional[str]:
    desc = item.get("description") or ""
    for line in desc.splitlines():
        if line.lower().startswith("source:"):
            url = line.split(":", 1)[1].strip()
            return url
    return None


def _monitor_phase(venture: str, max_items: int) -> List[Dict[str, Any]]:
    """Call ``delimit_sensor_github_issue`` for each open outreach LED.

    Returns a list of monitor records. Each record carries:
      * ``item_id`` — LED id (e.g. ``LED-XXXX``)
      * ``repo`` / ``issue_number`` — parsed target
      * ``has_new_activity`` — sensor verdict
      * ``signal`` — sensor signal dict (when activity present)
      * ``error`` — exception text (when the call failed)

    The function never raises — sensor failures are recorded and the
    loop continues.
    """
    records: List[Dict[str, Any]] = []
    items = _open_intel_items(venture, max_items)
    if not items:
        return records
    try:
        from ai.governance import _sensor_github_issue_impl  # type: ignore
    except Exception:
        _sensor_github_issue_impl = None

    if _sensor_github_issue_impl is None:
        try:
            from backends.governance_bridge import sensor_github_issue as _sensor_github_issue_impl  # type: ignore
        except Exception as exc:
            logger.warning(
                "monitor_phase: sensor import failed (%s) — monitor skipped",
                exc,
            )
            return records

    for item in items:
        url = _extract_issue_url(item) or ""
        parsed = _parse_issue_url(url)
        if not parsed:
            continue
        repo, num = parsed
        try:
            signal = _sensor_github_issue_impl(repo=repo, issue_number=num)
        except Exception as exc:
            records.append({
                "item_id": item.get("id"),
                "repo": repo,
                "issue_number": num,
                "has_new_activity": False,
                "error": str(exc),
            })
            continue
        records.append({
            "item_id": item.get("id"),
            "repo": repo,
            "issue_number": num,
            "has_new_activity": bool(signal.get("has_new_activity")),
            "signal": signal,
        })
    return records


# ---------------------------------------------------------------------------
# Scan phase
# ---------------------------------------------------------------------------


def _scan_phase(
    venture: str,
    dispatch_cap: int,
) -> Dict[str, Any]:
    """Run the github scanner and let ``process_targets`` fire dispatches.

    Returns a dict with:
      * ``targets_scanned`` — count from ``scan_targets``
      * ``processed`` — full ``process_targets`` result
      * ``dispatches`` — list of agent_tasks emitted (capped to
        ``dispatch_cap``; targets beyond cap are scanned but not
        dispatched — their intel items still file normally)
      * ``cap_hit`` — bool
      * ``error`` — exception text on failure
    """
    out: Dict[str, Any] = {
        "targets_scanned": 0,
        "processed": {},
        "dispatches": [],
        "cap_hit": False,
    }
    try:
        from ai.social_target import scan_targets, process_targets
    except Exception as exc:
        out["error"] = f"social_target import failed: {exc}"
        return out

    # LED-2214b followup: pass limit=30 so the github scanner's phase-2
    # issue search actually runs. Default limit=10 lets phase-1 repo
    # search saturate first (each query returns up to 10 repos), so
    # phase-2 issue targets — the only kind that can carry technical
    # anchors in their bodies — never reach the gate. With limit=30
    # we typically see ~20 repos + ~4-6 issues per tick, which the
    # per-tick dispatch cap (3) further constrains downstream.
    try:
        targets = scan_targets(platforms=["github"], venture=venture, limit=30) or []
    except Exception as exc:
        out["error"] = f"scan_targets failed: {exc}"
        return out
    out["targets_scanned"] = len(targets)

    # Apply per-tick cap by truncating targets BEFORE process_targets so
    # the fan-out cap is enforced in the daemon, not deep inside the
    # scanner. The reason: process_targets is also called by the
    # general social_daemon path, which has its own cap; mixing both
    # caps inside process_targets would couple the two daemons.
    #
    # LED-2214b followup: sort issue targets to the FRONT before the
    # truncation. The scanner returns phase-1 repo discoveries before
    # phase-2 issue results, so a naive `targets[:3]` strips out the
    # only target shape that can carry technical anchors. Issue-first
    # ordering ensures the dispatch cap doesn't waste budget on
    # repo-discovery targets that will all be rejected as anchor-less.
    if dispatch_cap > 0 and len(targets) > dispatch_cap:
        out["cap_hit"] = True
        targets.sort(
            key=lambda t: 0 if (t.get("fingerprint", "") or "").startswith("github:issue:") else 1
        )
        targets = targets[:dispatch_cap]

    try:
        processed = process_targets(
            targets, draft_replies=True, create_ledger=True,
        )
    except Exception as exc:
        out["error"] = f"process_targets failed: {exc}"
        return out
    out["processed"] = processed
    out["dispatches"] = list(processed.get("agent_tasks", []))
    return out


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def tick(
    venture: str = "delimit",
    max_dispatch: int = DEFAULT_MAX_DISPATCH,
    max_monitor: int = DEFAULT_MAX_MONITOR,
) -> Dict[str, Any]:
    """Run one outreach-loop cycle.

    Args:
        venture: Sourcing venture (default ``delimit``).
        max_dispatch: Per-tick cap on substantive dispatches. The
            scanner may surface more targets than this; the excess
            still files intel-class LEDs via ``process_targets`` on
            subsequent ticks. Set to ``0`` to disable the cap (not
            recommended — the cap is the spam-loop firewall).
        max_monitor: Per-tick cap on monitor calls (one
            ``delimit_sensor_github_issue`` per open outreach LED).

    Returns:
        Dict with ``venture``, ``started_at``, ``ended_at``,
        ``kill_switch`` (active flag + reason), ``monitor`` (list of
        per-LED records), ``scan`` (full scan-phase summary), and
        ``dispatch_count``.
    """
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    summary: Dict[str, Any] = {
        "venture": venture,
        "started_at": started_at,
        "max_dispatch": max_dispatch,
        "max_monitor": max_monitor,
    }
    active, reason = kill_switch_active()
    summary["kill_switch"] = {"active": active, "reason": reason}
    if active:
        summary["status"] = "skipped"
        summary["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        logger.info("outreach_loop tick skipped: kill switch active (%s)", reason)
        return summary

    summary["monitor"] = _monitor_phase(venture=venture, max_items=max_monitor)
    summary["scan"] = _scan_phase(venture=venture, dispatch_cap=max_dispatch)
    summary["dispatch_count"] = len(summary["scan"].get("dispatches") or [])
    summary["status"] = "ok"
    summary["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    logger.info(
        "outreach_loop tick ok: venture=%s monitored=%d dispatched=%d cap_hit=%s",
        venture,
        len(summary["monitor"]),
        summary["dispatch_count"],
        summary["scan"].get("cap_hit"),
    )
    return summary
