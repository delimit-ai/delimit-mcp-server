"""
Self-repair KPI watcher daemon (alert-mode MVP).

Responsibilities:
  - Periodically evaluate every function whose mode is >= ALERT.
  - Emit a `delimit_notify` email on each fresh breach.
  - Enforce a per-function rate limit (default 3 alerts per 24h) so a
    chronic breach doesn't spam the founder inbox.
  - Dedupe identical (function, kpi_name) breaches inside a configurable
    re-fire window (default 24h).
  - Respect the global pause flag and per-function pause flag at every
    pass — a pause flipped on while the daemon is running is honored on
    the next pass.

NO diagnose / deliberate / apply / verify code lives here. Higher modes
are intentionally out of scope for the MVP.

State is persisted to `~/.delimit/self_repair_state.json` so the
dedupe / rate-limit windows survive daemon restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .kpi import (
    Breach,
    evaluate_function,
    extract_breaches,
    load_function_kpis,
)
from .mode import Mode, get_mode, load_config
from .diagnose import (
    DiagnosticBundle,
    gather_diagnostic,
    render_text as render_diagnostic_text,
)
from .deliberate import (
    DEFAULT_DELIBERATION_TIMEOUT_SECONDS,
    DeliberationVerdict,
    render_verdict_email,
    run_deliberation,
)
from .history import (
    DEFAULT_HISTORY_PATH,
    append_history,
    deliberations_this_week,
)
from .apply import unpause_expired_disables
from .verify import (
    DEFAULT_VERIFY_QUEUE_PATH,
    VerifyOutcome,
    run_due_verifies,
)

logger = logging.getLogger("delimit.ai.self_repair.watcher")

STATE_PATH = Path.home() / ".delimit" / "self_repair_state.json"

# Defaults if config doesn't override.
_DEFAULT_MAX_ALERTS_PER_DAY = 3
_DEFAULT_DEDUP_WINDOW_HOURS = 24
_DEFAULT_PASS_INTERVAL_SECONDS = 3600  # one hour
_DEFAULT_MAX_DELIBERATIONS_PER_WEEK = 3
# LED-1210: per-KPI cap. 1/week is the safe default — a single KPI
# breach should produce at most one deliberation per 7d window. The
# function-level cap (3/week) acts as the upstream ceiling.
_DEFAULT_MAX_DELIBERATIONS_PER_KPI_PER_WEEK = 1
# LED-1210: throttle the throttled-notify itself so a chronic breach
# doesn't spam the founder with "would have deliberated" emails. One
# throttle notice per (function) per 24h is plenty.
_DEFAULT_THROTTLE_NOTICE_INTERVAL_HOURS = 24


# ── notify entry-point resolution ────────────────────────────────────


def _default_notify(
    *,
    subject: str,
    body: str,
    severity: str,
) -> Dict[str, Any]:
    """Resolve and call the gateway's notify entry-point.

    The MCP tool surface is `delimit_notify`. The underlying Python
    implementation in `ai.notify` is `send_notification(...)`. We call
    that directly so the watcher works headless (no MCP transport
    required when running as a systemd unit).

    If the import fails (e.g. running in a stripped-down test env), we
    fall back to appending to a JSONL queue file so the dispatcher can
    pick it up later — and log a clear TODO.
    """
    try:
        from ai.notify import send_notification  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time fallback
        logger.warning(
            "self_repair: cannot import ai.notify.send_notification (%s) — "
            "falling back to queue file. TODO: ensure dispatcher drains "
            "self_repair_queue.jsonl",
            exc,
        )
        queue = Path.home() / ".delimit" / "self_repair_queue.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        with open(queue, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(tz=timezone.utc).isoformat(),
                        "subject": subject,
                        "body": body,
                        "severity": severity,
                    }
                )
                + "\n"
            )
        return {"queued": True, "queue": str(queue)}

    return send_notification(
        channel="email",
        subject=subject,
        message=body,
        event_type="self_repair_breach",
    )


# ── state tracking ───────────────────────────────────────────────────


@dataclass
class _BreachState:
    """Per-(function, kpi) book-keeping persisted across daemon runs."""

    last_fired_at: Optional[str] = None  # ISO-8601 UTC, populated on alert
    fire_count_window: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.fire_count_window is None:
            self.fire_count_window = []


def _load_state(path: Path = STATE_PATH) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("self_repair: cannot read state %s: %s", path, exc)
        return {}


def _save_state(
    state: Dict[str, Dict[str, Any]], path: Path = STATE_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _state_key(function: str, kpi_name: str) -> str:
    return f"{function}::{kpi_name}"


def _prune_window(
    timestamps: List[str], *, now: datetime, window: timedelta
) -> List[str]:
    cutoff = now - window
    out: List[str] = []
    for ts_raw in timestamps:
        try:
            ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(ts_raw)
    return out


# ── daemon ───────────────────────────────────────────────────────────


@dataclass
class WatcherDaemon:
    """KPI watcher process.

    Construction is cheap and side-effect free — actual work happens in
    `run_one_pass()` (single-shot) or `run_forever()` (long-lived).

    `notify_fn` is dependency-injected so tests can stub the email path.

    `diagnose_paths` is an optional dict mapping
    `{audit_dir, cron_log, ledger_dir, state_path}` → Path so tests can
    point the diagnose-mode evidence gatherer at fixture directories
    instead of the real `~/.delimit/`. When unset, diagnose falls back
    to its module-level defaults.
    """

    config_path: Optional[Path] = None
    state_path: Path = STATE_PATH
    notify_fn: Callable[..., Dict[str, Any]] = _default_notify
    pass_interval_seconds: int = _DEFAULT_PASS_INTERVAL_SECONDS
    base_kpi_dir: Optional[Path] = None
    sources_base_dir: Optional[Path] = None  # for tests / fixture mode
    diagnose_paths: Optional[Dict[str, Path]] = None
    # Deliberate-mode dependency injection. Tests pass a stub
    # `deliberate_fn` that returns a canned panel dict; in production
    # this falls through to `ai.deliberation.deliberate` via the
    # default in `deliberate.py`.
    deliberate_fn: Optional[Callable[..., Dict[str, Any]]] = None
    deliberation_timeout_seconds: int = DEFAULT_DELIBERATION_TIMEOUT_SECONDS
    history_path: Optional[Path] = None
    history_dir: Optional[Path] = None  # transcript root override (testing)
    # Apply / verify layer paths (None → defaults under ~/.delimit/).
    verify_queue_path: Optional[Path] = None
    apply_log_path: Optional[Path] = None
    overrides_dir: Optional[Path] = None

    # ── public api ──────────────────────────────────────────────────

    def run_one_pass(
        self,
        functions: Optional[List[str]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> List[Breach]:
        """Evaluate every enabled function once. Returns breaches that
        fired notifications during this pass.
        """
        now = now or datetime.now(tz=timezone.utc)

        # Hard env override — short-circuits everything.
        if os.environ.get("DELIMIT_SELF_REPAIR_PAUSE", "").strip() == "1":
            logger.info("self_repair: global pause flag set; pass skipped")
            return []

        # Apply-layer hook: unpause any function whose disable_temp
        # window has elapsed. Done BEFORE config-pause check so that a
        # global pause does not trap individual functions in disable_temp
        # forever.
        try:
            unpaused = unpause_expired_disables(
                now=now, config_path=self.config_path
            )
            if unpaused:
                logger.info(
                    "self_repair: unpaused expired disable_temp for: %s",
                    unpaused,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "self_repair: unpause_expired_disables raised: %s", exc
            )

        config = load_config(self.config_path)

        if bool(config.get("pause", False)):
            logger.info("self_repair: config pause flag set; pass skipped")
            # Even when paused, run verifies — recovery measurements are
            # cheap reads and the loop should still close on already-
            # applied fixes.
            self._run_verifies(now=now)
            return []

        target_fns = functions or self._enumerate_functions(config)
        state = _load_state(self.state_path)
        fired: List[Breach] = []

        for fn in target_fns:
            mode = get_mode(fn, config)
            if mode < Mode.ALERT:
                continue

            try:
                results = evaluate_function(
                    fn,
                    base_dir=self.base_kpi_dir,
                    now=now,
                    sources_base_dir=self.sources_base_dir,
                )
            except FileNotFoundError as exc:
                logger.warning(
                    "self_repair: no KPI declaration for %s — %s", fn, exc
                )
                continue
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "self_repair: KPI evaluation failed for %s: %s", fn, exc
                )
                continue

            breaches = extract_breaches(results, function=fn)
            if not breaches:
                continue

            # Load the function yaml once for this function — used by
            # diagnose-mode rendering. We tolerate failure here because
            # alert-mode operation does not require the yaml beyond
            # what evaluate_function already used internally.
            function_yaml: Dict[str, Any] = {}
            if mode >= Mode.DIAGNOSE:
                try:
                    function_yaml = load_function_kpis(
                        fn, base_dir=self.base_kpi_dir
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "self_repair: cannot load yaml for diagnose on %s: %s",
                        fn,
                        exc,
                    )
                    function_yaml = {}

            fn_cfg = (config.get("functions") or {}).get(fn) or {}
            rate_limits = fn_cfg.get("rate_limits") or {}
            max_alerts = int(
                rate_limits.get(
                    "max_alerts_per_day",
                    rate_limits.get(
                        "max_deliberations_per_week",
                        _DEFAULT_MAX_ALERTS_PER_DAY,
                    ),
                )
            )
            dedup_hours = int(
                rate_limits.get(
                    "dedup_window_hours", _DEFAULT_DEDUP_WINDOW_HOURS
                )
            )
            # Deliberate-mode has its own (slower) rate limit. We default
            # to 3/week to match `default_self_repair.yaml` and avoid
            # storming the panel.
            max_delibs_per_week = int(
                rate_limits.get(
                    "max_deliberations_per_week",
                    rate_limits.get(
                        # Tolerate the alternate name used in the
                        # LED-1210 directive draft so external configs
                        # using either spelling work.
                        "max_deliberations_per_function_per_week",
                        _DEFAULT_MAX_DELIBERATIONS_PER_WEEK,
                    ),
                )
            )
            # LED-1210: per-KPI cap. Defaults to 1/week if not declared
            # in yaml (safe default). Treated as an ADDITIONAL constraint
            # on top of the function-level cap, not a replacement.
            max_delibs_per_kpi_per_week = int(
                rate_limits.get(
                    "max_deliberations_per_kpi_per_week",
                    _DEFAULT_MAX_DELIBERATIONS_PER_KPI_PER_WEEK,
                )
            )

            for b in breaches:
                if self._should_suppress(
                    state,
                    b,
                    now=now,
                    max_alerts=max_alerts,
                    dedup_hours=dedup_hours,
                ):
                    continue
                self._fire(
                    b,
                    now=now,
                    mode=mode,
                    function_yaml=function_yaml,
                    max_delibs_per_week=max_delibs_per_week,
                    max_delibs_per_kpi_per_week=max_delibs_per_kpi_per_week,
                    state=state,
                )
                self._record_fire(state, b, now=now)
                fired.append(b)

        _save_state(state, self.state_path)

        # Verify-layer hook: re-measure any KPI whose post-fix verify
        # window has elapsed. Done LAST so a verify-driven mode demotion
        # does not prevent the breach detection above from running on
        # the same pass.
        self._run_verifies(now=now)
        return fired

    def _run_verifies(self, *, now: datetime) -> List[VerifyOutcome]:
        """Run any due verify tasks. Failure-isolated — a broken verify
        queue must never stall the watcher's main breach-detection
        loop. Returns the outcomes for diagnostics."""
        try:
            return run_due_verifies(
                now=now,
                queue_path=self.verify_queue_path,
                history_path=self.history_path,
                config_path=self.config_path,
                sources_base_dir=self.sources_base_dir,
                base_kpi_dir=self.base_kpi_dir,
                notify_fn=self._verify_notify,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("self_repair: run_due_verifies raised: %s", exc)
            return []

    def _verify_notify(
        self,
        *,
        subject: str,
        body: str,
        severity: str,
    ) -> Dict[str, Any]:
        """Bridge verify outcomes through the same notify_fn the
        watcher uses for breach alerts. Keeps the email path uniform —
        an operator who can read the breach inbox can also read the
        recovery / escalate emails without a separate channel."""
        try:
            return self.notify_fn(
                subject=subject, body=body, severity=severity
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("self_repair: verify notify_fn raised: %s", exc)
            return {"error": str(exc)}

    def run_forever(
        self,
        functions: Optional[List[str]] = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_passes: Optional[int] = None,
    ) -> None:
        """Run `run_one_pass` on a loop. `max_passes` lets tests bound it."""
        passes = 0
        while True:
            try:
                self.run_one_pass(functions=functions)
            except Exception:  # pragma: no cover - defensive
                logger.exception("self_repair: pass failed; continuing")
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return
            sleeper(self.pass_interval_seconds)

    # ── internals ───────────────────────────────────────────────────

    def _enumerate_functions(self, config: Dict[str, Any]) -> List[str]:
        """Functions named in config get evaluated every pass."""
        fns = config.get("functions") or {}
        if isinstance(fns, dict):
            return sorted(fns.keys())
        return []

    def _should_suppress(
        self,
        state: Dict[str, Dict[str, Any]],
        breach: Breach,
        *,
        now: datetime,
        max_alerts: int,
        dedup_hours: int,
    ) -> bool:
        key = _state_key(breach.function, breach.kpi_name)
        entry = state.get(key) or {}
        last = entry.get("last_fired_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.rstrip("Z"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if now - last_dt < timedelta(hours=dedup_hours):
                    logger.debug(
                        "self_repair: dedup suppressing %s (last fired %s)",
                        key,
                        last,
                    )
                    return True
            except ValueError:
                pass

        window_starts = entry.get("fire_count_window") or []
        # 24h sliding window is fixed — separate from the dedup window.
        window_starts = _prune_window(
            window_starts, now=now, window=timedelta(hours=24)
        )
        if len(window_starts) >= max_alerts:
            logger.info(
                "self_repair: rate-limit suppressing %s (%d alerts in 24h)",
                key,
                len(window_starts),
            )
            entry["fire_count_window"] = window_starts
            state[key] = entry
            return True
        return False

    def _record_fire(
        self,
        state: Dict[str, Dict[str, Any]],
        breach: Breach,
        *,
        now: datetime,
    ) -> None:
        key = _state_key(breach.function, breach.kpi_name)
        entry = state.get(key) or {}
        ts_iso = now.isoformat()
        entry["last_fired_at"] = ts_iso
        window = entry.get("fire_count_window") or []
        window = _prune_window(window, now=now, window=timedelta(hours=24))
        window.append(ts_iso)
        entry["fire_count_window"] = window
        state[key] = entry

    def _fire(
        self,
        breach: Breach,
        *,
        now: datetime,
        mode: Mode = Mode.ALERT,
        function_yaml: Optional[Dict[str, Any]] = None,
        max_delibs_per_week: int = _DEFAULT_MAX_DELIBERATIONS_PER_WEEK,
        max_delibs_per_kpi_per_week: int = (
            _DEFAULT_MAX_DELIBERATIONS_PER_KPI_PER_WEEK
        ),
        state: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send the notify for `breach`.

        Mode dispatch:
          alert       - simple notify, no diagnose
          diagnose    - notify with the 30d evidence bundle appended
          deliberate  - notify with the panel verdict + bundle
                        highlights; verdict is recorded in
                        self_repair_history.jsonl

        Each higher mode falls through to the next-lowest on failure so
        the founder always hears about the breach. Specifically:

          - diagnose-mode failure → alert-mode email
          - deliberate-mode failure or timeout → diagnose-mode email
          - deliberate-mode rate-limited → throttled-mode email

        LED-1210: deliberate-mode is rate-limited at TWO levels:
          1. function-level: `max_deliberations_per_week` (existing)
          2. per-KPI:        `max_deliberations_per_kpi_per_week` (new)
        Either limit being exceeded throttles the deliberation. The
        per-KPI cap defaults to 1/week and is treated as an ADDITIONAL
        constraint, not a replacement, so existing rate-limit
        consumers keep working.

        The throttle notice itself is rate-limited to once per (function)
        per 24h via `_should_emit_throttle_notice` so a chronic breach
        doesn't spam the founder inbox with "would have deliberated"
        emails.
        """
        is_diagnose = mode >= Mode.DIAGNOSE
        is_deliberate = mode >= Mode.DELIBERATE

        subject = self._build_alert_subject(breach, mode)
        bundle: Optional[DiagnosticBundle] = None
        if is_diagnose:
            try:
                paths = self.diagnose_paths or {}
                bundle = gather_diagnostic(
                    breach,
                    function_yaml or {},
                    now=now,
                    sources_base_dir=self.sources_base_dir,
                    audit_dir=paths.get("audit_dir"),
                    cron_log=paths.get("cron_log"),
                    ledger_dir=paths.get("ledger_dir"),
                    state_path=paths.get("state_path") or self.state_path,
                )
            except Exception as exc:
                logger.warning(
                    "self_repair: diagnose failed for %s; falling back to "
                    "alert-mode notify (%s)",
                    breach.kpi_name,
                    exc,
                )
                bundle = None
                # Reset the subject so the email doesn't claim
                # "+ evidence" when there is none.
                subject = (
                    f"[self-repair] {breach.function} breach: {breach.kpi_name}"
                )

        # Deliberate-mode runs only when (a) we are in mode>=DELIBERATE,
        # (b) we successfully gathered a bundle (deliberation requires
        # evidence), and (c) BOTH the function-level and per-KPI rate
        # limits are not exhausted. Any failure / timeout falls through
        # to diagnose-mode.
        verdict: Optional[DeliberationVerdict] = None
        throttled = False
        throttle_reason = ""
        throttle_count = 0
        throttle_cap = 0
        if is_deliberate and bundle is not None:
            fn_count = deliberations_this_week(
                breach.function,
                now=now,
                history_path=self.history_path,
            )
            kpi_count = deliberations_this_week(
                breach.function,
                now=now,
                history_path=self.history_path,
                kpi=breach.kpi_name,
            )
            if kpi_count >= max_delibs_per_kpi_per_week:
                # Per-KPI cap is the inner limit — check it first so the
                # throttle notice names the actual blocker accurately.
                throttled = True
                throttle_reason = "kpi"
                throttle_count = kpi_count
                throttle_cap = max_delibs_per_kpi_per_week
                logger.info(
                    "self_repair: deliberate-mode per-KPI rate limit hit "
                    "for %s::%s (%d/%d this week); emitting throttled notify",
                    breach.function,
                    breach.kpi_name,
                    kpi_count,
                    max_delibs_per_kpi_per_week,
                )
            elif fn_count >= max_delibs_per_week:
                throttled = True
                throttle_reason = "function"
                throttle_count = fn_count
                throttle_cap = max_delibs_per_week
                logger.info(
                    "self_repair: deliberate-mode function rate limit hit "
                    "for %s (%d/%d this week); emitting throttled notify",
                    breach.function,
                    fn_count,
                    max_delibs_per_week,
                )
            else:
                verdict = self._run_deliberation_with_timeout(
                    breach=breach,
                    bundle=bundle,
                    function_yaml=function_yaml or {},
                    now=now,
                )
                if verdict is not None:
                    try:
                        append_history(verdict, history_path=self.history_path)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "self_repair: append_history failed for %s: %s",
                            breach.kpi_name,
                            exc,
                        )

        # Throttle-notice rate-limit. If we'd emit a throttled email
        # but already sent one for this function within the last 24h,
        # suppress entirely so we don't spam the founder with "would
        # have deliberated" notices.
        if throttled and state is not None:
            if not self._should_emit_throttle_notice(state, breach, now=now):
                logger.info(
                    "self_repair: throttle-notice itself rate-limited for "
                    "%s (last notice <24h ago); suppressing email",
                    breach.function,
                )
                return {"throttled": True, "suppressed_notice": True}
            self._record_throttle_notice(state, breach, now=now)

        # Compose the outgoing email body. Order of preference:
        #   1. deliberate-mode verdict body (if verdict is non-None)
        #   2. throttled notice (if rate-limited)
        #   3. diagnose-mode body (if bundle is non-None)
        #   4. alert-mode body
        if verdict is not None:
            subject, body = render_verdict_email(verdict)
        elif throttled:
            subject = (
                f"[self-repair-deliberate-throttled] {breach.function} "
                f"breach: {breach.kpi_name}"
            )
            body = self._render_throttled_body(
                breach,
                now=now,
                bundle=bundle,
                count=throttle_count,
                cap=throttle_cap,
                reason=throttle_reason,
            )
        else:
            body = self._render_body(
                breach, now=now, mode=mode, bundle=bundle
            )

        try:
            return self.notify_fn(
                subject=subject, body=body, severity=breach.severity
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "self_repair: notify_fn raised for %s: %s",
                breach.kpi_name,
                exc,
            )
            return {"error": str(exc)}

    @staticmethod
    def _throttle_state_key(function: str) -> str:
        # Reserve a state-key namespace for throttle bookkeeping so it
        # can't collide with the (function, kpi) breach-state keys.
        return f"__throttle__::{function}"

    def _should_emit_throttle_notice(
        self,
        state: Dict[str, Dict[str, Any]],
        breach: Breach,
        *,
        now: datetime,
    ) -> bool:
        """LED-1210: rate-limit the throttle notice itself.

        Returns True when no throttle notice has been emitted for this
        function within `_DEFAULT_THROTTLE_NOTICE_INTERVAL_HOURS`.
        """
        key = self._throttle_state_key(breach.function)
        entry = state.get(key) or {}
        last = entry.get("last_throttle_notice_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last).rstrip("Z"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return (now - last_dt) >= timedelta(
            hours=_DEFAULT_THROTTLE_NOTICE_INTERVAL_HOURS
        )

    def _record_throttle_notice(
        self,
        state: Dict[str, Dict[str, Any]],
        breach: Breach,
        *,
        now: datetime,
    ) -> None:
        key = self._throttle_state_key(breach.function)
        entry = state.get(key) or {}
        entry["last_throttle_notice_at"] = now.isoformat()
        state[key] = entry

    def _build_alert_subject(self, breach: Breach, mode: Mode) -> str:
        """Subject for the diagnose / alert paths. The deliberate path
        constructs its own subject inside `render_verdict_email`."""
        if mode >= Mode.DIAGNOSE:
            return (
                f"[self-repair-diagnose] {breach.function} breach: "
                f"{breach.kpi_name} + evidence"
            )
        return f"[self-repair] {breach.function} breach: {breach.kpi_name}"

    def _run_deliberation_with_timeout(
        self,
        *,
        breach: Breach,
        bundle: DiagnosticBundle,
        function_yaml: Dict[str, Any],
        now: datetime,
    ) -> Optional[DeliberationVerdict]:
        """Run `run_deliberation` with a hard timeout.

        Returns the verdict on success, None on timeout or hard error.
        Caller falls through to diagnose-mode email when None.

        We use a daemon thread + Event for the timeout so the watcher
        can keep working even if the underlying engine hangs (panel
        responses can stall on a single slow model). signal.alarm is
        not used because the watcher may run inside a non-main thread
        or systemd unit where SIGALRM behaviour is awkward.
        """
        result: Dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                result["verdict"] = run_deliberation(
                    breach,
                    bundle,
                    function_yaml,
                    deliberate_fn=self.deliberate_fn,
                    history_dir=self.history_dir,
                    now=now,
                )
            except Exception as exc:  # pragma: no cover - defensive
                result["error"] = exc
                logger.warning(
                    "self_repair: run_deliberation raised for %s: %s",
                    breach.kpi_name,
                    exc,
                )
            finally:
                done.set()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        finished = done.wait(timeout=self.deliberation_timeout_seconds)
        if not finished:
            logger.warning(
                "self_repair: deliberation for %s timed out after %ds — "
                "falling through to diagnose-mode email",
                breach.kpi_name,
                self.deliberation_timeout_seconds,
            )
            return None
        if "error" in result:
            return None
        return result.get("verdict")

    @staticmethod
    def _render_throttled_body(
        breach: Breach,
        *,
        now: datetime,
        bundle: Optional[DiagnosticBundle],
        count: int,
        cap: int,
        reason: str = "function",
    ) -> str:
        """Email body for a deliberate-mode rate-limit suppression.

        We still surface the breach + (if available) the bundle so the
        founder is not blind during the throttling window. The subject
        prefix `[self-repair-deliberate-throttled]` makes the
        suppression auditable.

        LED-1210: `reason` is "kpi" (per-KPI cap hit) or "function"
        (function-wide cap hit). The body names the actual blocker and
        the relevant config knob so the founder can raise either limit
        independently.
        """
        if reason == "kpi":
            scope = f"per-KPI ({breach.kpi_name})"
            knob = "max_deliberations_per_kpi_per_week"
        else:
            scope = "per-function"
            knob = "max_deliberations_per_week"

        lines: List[str] = [
            "Self-repair would have deliberated this breach but the "
            f"{scope} weekly deliberation rate limit ({cap}) is "
            f"exhausted (current: {count}/{cap}).",
            "",
            f"Function:   {breach.function}",
            f"KPI:        {breach.kpi_name}",
            f"Severity:   {breach.severity}",
            f"Window:     {breach.window}",
            f"Actual:     {breach.actual}",
            f"Threshold:  {breach.threshold}",
            f"Detected:   {now.isoformat()}",
            "",
            "Rationale:",
            breach.rationale or "(none provided)",
        ]
        if bundle is not None:
            lines.append("")
            lines.append("=== Diagnostic bundle (deliberation suppressed) ===")
            lines.append(render_diagnostic_text(bundle))
        lines.append("")
        lines.append(
            "To force a deliberation despite the rate limit, raise "
            f"`rate_limits.{knob}` in ~/.delimit/self_repair.yaml or "
            "run `delimit self-repair deliberate <function>` manually."
        )
        return "\n".join(lines)

    @staticmethod
    def _render_body(
        breach: Breach,
        *,
        now: datetime,
        mode: Mode = Mode.ALERT,
        bundle: Optional[DiagnosticBundle] = None,
    ) -> str:
        lines = [
            f"Self-repair watcher detected a KPI breach.",
            "",
            f"Function:   {breach.function}",
            f"KPI:        {breach.kpi_name}",
            f"Severity:   {breach.severity}",
            f"Window:     {breach.window}",
            f"Actual:     {breach.actual}",
            f"Threshold:  {breach.threshold}",
            f"Detected:   {now.isoformat()}",
            "",
            "Rationale:",
            breach.rationale or "(none provided)",
            "",
            "Source data:",
            json.dumps(breach.source_data_summary, indent=2, default=str),
        ]

        if bundle is not None:
            lines.append("")
            lines.append("=" * 64)
            lines.append("DIAGNOSTIC BUNDLE (mode=diagnose)")
            lines.append("=" * 64)
            lines.append("")
            lines.append(render_diagnostic_text(bundle))
        else:
            lines.append("")
            lines.append(
                f"Mode: {mode.value} (no diagnose / deliberate / apply runs at this mode)."
            )
            lines.append("Reply to this email to trigger founder-side action.")
        return "\n".join(lines)


# Convenience top-level for callers that just want one pass.
def run_one_pass(
    functions: Optional[List[str]] = None,
    *,
    config_path: Optional[Path] = None,
    state_path: Path = STATE_PATH,
    notify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    base_kpi_dir: Optional[Path] = None,
    sources_base_dir: Optional[Path] = None,
    diagnose_paths: Optional[Dict[str, Path]] = None,
    deliberate_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    history_path: Optional[Path] = None,
    history_dir: Optional[Path] = None,
    deliberation_timeout_seconds: int = DEFAULT_DELIBERATION_TIMEOUT_SECONDS,
    verify_queue_path: Optional[Path] = None,
    apply_log_path: Optional[Path] = None,
    overrides_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> List[Breach]:
    """Functional shortcut for one-shot evaluation (used by the CLI)."""
    daemon = WatcherDaemon(
        config_path=config_path,
        state_path=state_path,
        notify_fn=notify_fn or _default_notify,
        base_kpi_dir=base_kpi_dir,
        sources_base_dir=sources_base_dir,
        diagnose_paths=diagnose_paths,
        deliberate_fn=deliberate_fn,
        history_path=history_path,
        history_dir=history_dir,
        deliberation_timeout_seconds=deliberation_timeout_seconds,
        verify_queue_path=verify_queue_path,
        apply_log_path=apply_log_path,
        overrides_dir=overrides_dir,
    )
    return daemon.run_one_pass(functions=functions, now=now)


def breach_to_dict(b: Breach) -> Dict[str, Any]:
    """Serialize a Breach for CLI / JSON output."""
    return asdict(b)
