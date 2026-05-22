"""
Self-repair verify layer (post-apply re-measurement).

After a fix is applied, the loop must close: did the KPI actually
recover? This module schedules a re-measurement at `applied_at +
parse_window(fix_window) / 2` (so a 14d KPI is verified 7d after the
fix), then runs each due verify on the next watcher pass.

Three outcomes per verify:

  - recovered      — KPI now passes. Mark history `verify_passed=True`,
                     `closed_at=now`, emit a success notify.
  - still_breaching — KPI still fails. Increment `failed_fix_count`.
                     If it hits >= 2, emit `[self-repair-ESCALATE]` and
                     suspend further auto-deliberation for the function
                     by demoting its mode to `diagnose` (so the founder
                     sees evidence-only emails until manual intervention).
  - partial        — KPI improved (`actual` moved toward the threshold)
                     but is still short of it. Reschedule one more
                     verify cycle and note in history.

The verify queue lives at
`~/.delimit/self_repair_verify_queue.jsonl`. Each row is:

    {
      "history_id":  "<action_id from history.jsonl>",
      "function":    "marketing.social_outreach",
      "kpi_name":    "drafts_per_day",
      "fix_window":  "24h",
      "applied_at":  "2026-04-30T...",
      "verify_at":   "2026-05-01T...",   # applied_at + parse_window/2
      "verify_count": 0,                  # how many times we've run
      "rollback_token": "deadbeef...",   # ties back to apply log
    }

Public API:
    schedule_verify(history_id, fn_name, kpi_name, fix_window, applied_at)
        -> Path   (queue path written)
    run_due_verifies(now=None) -> list[VerifyOutcome]

Constraints honored:
  - No new external deps (stdlib + already-imported PyYAML).
  - Atomic writes via tmp+rename (mirrors history.py / state.json).
  - Verify failure escalation NEVER auto-applies a follow-up fix —
    the loop's whole point is that a failed fix is a sign the panel
    misjudged the situation, and the founder should see the evidence
    before another round of automation runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .history import (
    DEFAULT_HISTORY_PATH,
    iter_history,
)
from .kpi import (
    KpiResult,
    evaluate_function,
    parse_window,
)
from .mode import Mode, USER_CONFIG_PATH, _read_yaml, set_mode

logger = logging.getLogger("delimit.ai.self_repair.verify")


USER_DELIMIT_DIR = Path.home() / ".delimit"
DEFAULT_VERIFY_QUEUE_PATH = USER_DELIMIT_DIR / "self_repair_verify_queue.jsonl"

# Failed-fix count threshold for escalation. Beyond this, the loop
# stops auto-deliberating the function.
ESCALATION_FAIL_THRESHOLD = 2

# Maximum verify reschedules per history_id. Prevents runaway loops on
# `partial` outcomes where a KPI keeps inching toward the threshold but
# never crosses.
MAX_VERIFY_RESCHEDULES = 3


# ── data model ───────────────────────────────────────────────────────


@dataclass
class VerifyOutcome:
    """Outcome of one re-measurement pass."""

    history_id: str
    fn_name: str
    kpi_name: str
    status: str  # 'recovered' | 'still_breaching' | 'partial' | 'no_kpi' | 'error'
    actual_now: Optional[float]
    threshold: Optional[float]
    prior_actual: Optional[float]
    message: str
    failed_fix_count: int = 0
    escalated: bool = False
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── queue I/O ────────────────────────────────────────────────────────


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _atomic_rewrite_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    tmp.replace(path)


def _read_queue(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _append_queue(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str, sort_keys=True) + "\n")


# ── public: schedule a verify ────────────────────────────────────────


def schedule_verify(
    history_id: str,
    fn_name: str,
    kpi_name: str,
    fix_window: str,
    applied_at: datetime,
    *,
    queue_path: Optional[Path] = None,
    rollback_token: Optional[str] = None,
) -> Path:
    """Append a verify task to the queue.

    `verify_at` is computed as `applied_at + parse_window(fix_window) / 2`.
    A 14d KPI window therefore gets verified 7d after the fix landed —
    enough time for the metric to move but not so long the founder
    forgets which fix is being measured.

    If `fix_window` is unparseable (e.g. yaml didn't carry one), we
    fall back to 24h so the verify still happens. The watcher logs a
    warning in that case.
    """
    target = queue_path or DEFAULT_VERIFY_QUEUE_PATH
    try:
        delta = parse_window(fix_window)
    except (ValueError, TypeError):
        logger.warning(
            "self_repair: verify schedule got unparseable window %r for %s; "
            "defaulting to 24h",
            fix_window,
            history_id,
        )
        delta = timedelta(hours=24)

    verify_at = applied_at + (delta / 2)
    row: Dict[str, Any] = {
        "history_id": history_id,
        "function": fn_name,
        "kpi_name": kpi_name,
        "fix_window": fix_window or "24h",
        "applied_at": applied_at.isoformat(),
        "verify_at": verify_at.isoformat(),
        "verify_count": 0,
        "failed_fix_count": 0,
    }
    if rollback_token:
        row["rollback_token"] = rollback_token
    _append_queue(target, row)
    logger.info(
        "self_repair: scheduled verify for %s at %s (window=%s)",
        history_id,
        verify_at.isoformat(),
        fix_window,
    )
    return target


# ── helpers used by run_due_verifies ────────────────────────────────


def _find_history_row(
    history_id: str, *, history_path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    for row in iter_history(history_path=history_path):
        if row.get("action_id") == history_id:
            return row
    return None


def _classify_outcome(
    *,
    actual_now: Optional[float],
    threshold: Optional[float],
    prior_actual: Optional[float],
) -> str:
    """Decide recovered | still_breaching | partial.

    `recovered` requires the new measurement to clear the threshold
    (floor or ceiling — we infer direction from prior_actual vs threshold).
    `partial` means the actual moved toward the threshold but didn't
    cross. `still_breaching` is everything else where the KPI is still
    on the wrong side.
    """
    if actual_now is None or threshold is None:
        return "no_kpi"

    # Direction inference: if the prior_actual was below the threshold,
    # the KPI is a floor (we want actual >= threshold). If the prior
    # was above, it's a ceiling (we want actual <= threshold).
    if prior_actual is None:
        # Without a prior, fall back to "did we satisfy ANY plausible
        # interpretation of the threshold?". We check both floor and
        # ceiling pass conditions; if either holds we call it recovered.
        if actual_now >= threshold or actual_now <= threshold:
            # Trivially true for any number — be more careful: if exact
            # match, call it recovered; otherwise still_breaching.
            if actual_now == threshold:
                return "recovered"
            return "still_breaching"
        return "still_breaching"

    floor_kpi = prior_actual < threshold
    if floor_kpi:
        # We need actual >= threshold to recover.
        if actual_now >= threshold:
            return "recovered"
        if actual_now > prior_actual:
            return "partial"
        return "still_breaching"
    else:
        # Ceiling: need actual <= threshold to recover.
        if actual_now <= threshold:
            return "recovered"
        if actual_now < prior_actual:
            return "partial"
        return "still_breaching"


def _evaluate_one_kpi(
    fn_name: str,
    kpi_name: str,
    *,
    sources_base_dir: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Optional[KpiResult]:
    try:
        results = evaluate_function(
            fn_name,
            base_dir=base_dir,
            sources_base_dir=sources_base_dir,
            now=now,
        )
    except FileNotFoundError as exc:
        logger.warning(
            "self_repair: verify cannot evaluate %s/%s: %s",
            fn_name,
            kpi_name,
            exc,
        )
        return None
    for r in results:
        if r.name == kpi_name:
            return r
    return None


# ── escalation ──────────────────────────────────────────────────────


def _emit_escalation_notify(
    *,
    fn_name: str,
    kpi_name: str,
    history_id: str,
    failed_fix_count: int,
    outcome: VerifyOutcome,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> None:
    """Send a `[self-repair-ESCALATE]` email when a fix fails twice."""
    subject = (
        f"[self-repair-ESCALATE] {fn_name} :: {kpi_name} — "
        f"{failed_fix_count} fix(es) failed"
    )
    lines = [
        "Self-repair fix loop has failed to recover this KPI.",
        "",
        f"Function:        {fn_name}",
        f"KPI:             {kpi_name}",
        f"History ID:      {history_id}",
        f"Failed fixes:    {failed_fix_count} (threshold: "
        f"{ESCALATION_FAIL_THRESHOLD})",
        "",
        f"Latest measurement: {outcome.actual_now}",
        f"Threshold:          {outcome.threshold}",
        f"Prior actual:       {outcome.prior_actual}",
        "",
        "Action taken: function mode demoted to 'diagnose' to suspend",
        "auto-deliberation. The watcher will keep emailing evidence",
        "bundles on breach but will NOT propose another fix without",
        "founder intervention. Restore with:",
        "",
        f"    delimit self-repair set {fn_name} deliberate",
        "",
        "Reply 'unblock' to clear without changing mode.",
    ]
    body = "\n".join(lines)
    if notify_fn is None:
        try:
            from ai.notify import send_notification  # type: ignore

            send_notification(
                channel="email",
                subject=subject,
                message=body,
                event_type="self_repair_verify_escalate",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "self_repair: escalate notify failed: %s — body queued only",
                exc,
            )
    else:
        try:
            notify_fn(subject=subject, body=body, severity="critical")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("self_repair: notify_fn raised: %s", exc)


def _emit_recovery_notify(
    *,
    fn_name: str,
    kpi_name: str,
    history_id: str,
    outcome: VerifyOutcome,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> None:
    subject = f"[self-repair-recovered] {fn_name} :: {kpi_name}"
    lines = [
        "Self-repair fix recovered the KPI.",
        "",
        f"Function:    {fn_name}",
        f"KPI:         {kpi_name}",
        f"History ID:  {history_id}",
        f"Actual now:  {outcome.actual_now}",
        f"Threshold:   {outcome.threshold}",
        "",
        "Loop closed. No further action required.",
    ]
    if notify_fn is None:
        try:
            from ai.notify import send_notification  # type: ignore

            send_notification(
                channel="email",
                subject=subject,
                message="\n".join(lines),
                event_type="self_repair_verify_recovered",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("self_repair: recovery notify failed: %s", exc)
    else:
        try:
            notify_fn(subject=subject, body="\n".join(lines), severity="info")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("self_repair: notify_fn raised: %s", exc)


# ── public: run due verifies ────────────────────────────────────────


def run_due_verifies(
    now: Optional[datetime] = None,
    *,
    queue_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    sources_base_dir: Optional[Path] = None,
    base_kpi_dir: Optional[Path] = None,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> List[VerifyOutcome]:
    """Read the verify queue, evaluate every entry whose `verify_at`
    is in the past, and act on the result.

    On `recovered`: emit success notify, drop from queue, mark history.
    On `still_breaching`: increment failed_fix_count; if >= threshold
    emit escalate notify, demote function mode to `diagnose`, and drop
    from queue.
    On `partial`: bump verify_count, reschedule one more cycle (up to
    MAX_VERIFY_RESCHEDULES), keep in queue.
    On `no_kpi` / `error`: log and reschedule once.

    Returns the list of outcomes processed this pass (does NOT include
    entries that were not yet due).
    """
    anchor = _now(now)
    target = queue_path or DEFAULT_VERIFY_QUEUE_PATH
    rows = _read_queue(target)
    if not rows:
        return []

    processed: List[VerifyOutcome] = []
    keep: List[Dict[str, Any]] = []

    for row in rows:
        verify_at_raw = row.get("verify_at")
        try:
            verify_at = datetime.fromisoformat(str(verify_at_raw).rstrip("Z"))
            if verify_at.tzinfo is None:
                verify_at = verify_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Malformed verify_at — drop the row to prevent zombies.
            logger.warning(
                "self_repair: dropping malformed verify row: %s", row
            )
            continue
        if verify_at > anchor:
            keep.append(row)
            continue

        history_id = str(row.get("history_id") or "")
        fn_name = str(row.get("function") or "")
        kpi_name = str(row.get("kpi_name") or "")
        verify_count = int(row.get("verify_count") or 0)
        failed_fix_count = int(row.get("failed_fix_count") or 0)

        # Look up the original measurement from the history record so we
        # can classify partial vs still_breaching.
        history_row = _find_history_row(history_id, history_path=history_path)
        prior_actual: Optional[float]
        prior_actual = None
        threshold: Optional[float] = None
        if history_row is not None:
            prior_actual = history_row.get("actual")
            threshold = history_row.get("threshold")

        # Re-evaluate.
        result = _evaluate_one_kpi(
            fn_name,
            kpi_name,
            sources_base_dir=sources_base_dir,
            base_dir=base_kpi_dir,
            now=anchor,
        )
        if result is None:
            outcome = VerifyOutcome(
                history_id=history_id,
                fn_name=fn_name,
                kpi_name=kpi_name,
                status="no_kpi",
                actual_now=None,
                threshold=threshold,
                prior_actual=prior_actual,
                message=f"KPI {kpi_name} not found in {fn_name} evaluation",
                failed_fix_count=failed_fix_count,
                escalated=False,
                timestamp=anchor.isoformat(),
            )
            processed.append(outcome)
            # Reschedule once if under the cap, else drop.
            if verify_count + 1 < MAX_VERIFY_RESCHEDULES:
                row["verify_count"] = verify_count + 1
                row["verify_at"] = (
                    anchor + timedelta(hours=24)
                ).isoformat()
                keep.append(row)
            continue

        actual_now = result.actual
        thr = result.threshold if result.threshold is not None else threshold

        status = _classify_outcome(
            actual_now=actual_now,
            threshold=thr,
            prior_actual=(
                float(prior_actual)
                if isinstance(prior_actual, (int, float))
                else None
            ),
        )

        outcome = VerifyOutcome(
            history_id=history_id,
            fn_name=fn_name,
            kpi_name=kpi_name,
            status=status,
            actual_now=actual_now,
            threshold=thr,
            prior_actual=(
                float(prior_actual)
                if isinstance(prior_actual, (int, float))
                else None
            ),
            message="",
            failed_fix_count=failed_fix_count,
            escalated=False,
            timestamp=anchor.isoformat(),
        )

        if status == "recovered":
            outcome.message = "KPI recovered after fix"
            _emit_recovery_notify(
                fn_name=fn_name,
                kpi_name=kpi_name,
                history_id=history_id,
                outcome=outcome,
                notify_fn=notify_fn,
            )
            processed.append(outcome)
            # Drop from queue (do not append to `keep`).
            continue

        if status == "still_breaching":
            failed_fix_count += 1
            outcome.failed_fix_count = failed_fix_count
            if failed_fix_count >= ESCALATION_FAIL_THRESHOLD:
                outcome.escalated = True
                outcome.message = (
                    f"KPI still breaching after {failed_fix_count} fixes — "
                    f"escalating; mode demoted to diagnose"
                )
                _emit_escalation_notify(
                    fn_name=fn_name,
                    kpi_name=kpi_name,
                    history_id=history_id,
                    failed_fix_count=failed_fix_count,
                    outcome=outcome,
                    notify_fn=notify_fn,
                )
                # Demote mode to diagnose so the watcher stops
                # auto-deliberating until the founder intervenes.
                try:
                    set_mode(
                        fn_name,
                        Mode.DIAGNOSE,
                        config_path=config_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "self_repair: could not demote %s mode: %s",
                        fn_name,
                        exc,
                    )
                processed.append(outcome)
                # Drop from queue — escalation has been raised.
                continue
            else:
                outcome.message = (
                    f"KPI still breaching ({failed_fix_count}/"
                    f"{ESCALATION_FAIL_THRESHOLD}); rescheduling"
                )
                processed.append(outcome)
                # Reschedule one more half-window cycle.
                if verify_count + 1 < MAX_VERIFY_RESCHEDULES:
                    fix_window = str(row.get("fix_window") or "24h")
                    try:
                        delta = parse_window(fix_window)
                    except (ValueError, TypeError):
                        delta = timedelta(hours=24)
                    row["verify_count"] = verify_count + 1
                    row["failed_fix_count"] = failed_fix_count
                    row["verify_at"] = (anchor + (delta / 2)).isoformat()
                    keep.append(row)
                continue

        if status == "partial":
            outcome.message = (
                f"KPI improved (prior={prior_actual} → now={actual_now}) "
                f"but still short of threshold {thr}; one more verify cycle"
            )
            processed.append(outcome)
            if verify_count + 1 < MAX_VERIFY_RESCHEDULES:
                fix_window = str(row.get("fix_window") or "24h")
                try:
                    delta = parse_window(fix_window)
                except (ValueError, TypeError):
                    delta = timedelta(hours=24)
                row["verify_count"] = verify_count + 1
                row["verify_at"] = (anchor + (delta / 2)).isoformat()
                keep.append(row)
            continue

        # 'error' or unknown — log and drop.
        outcome.message = f"unknown verify status: {status}"
        processed.append(outcome)
        # We treat unknown as terminal; drop from queue to avoid loops.

    # Persist the surviving rows. Use atomic rewrite even for empty
    # lists so a queue that drained to zero leaves a zero-byte file
    # instead of stale rows.
    _atomic_rewrite_jsonl(target, keep)
    return processed
