"""
Self-repair fix-application gate (apply-mode).

Per the 2026-04-30 panel verdict, v1 of the self-repair loop has ZERO
auto-apply: every fix requires an explicit founder approval reply.
This module enforces that contract — `apply_fix` refuses to run unless
it is handed an approval record from the inbox executor.

Three hard gates run before any tier-specific executor fires:

  1. Escalation hard-stop: if the originating verdict has a non-empty
     `escalation_class`, refuse even with founder approval. These are
     constitutional rules (force-push, ruleset bypass, etc.) that the
     loop CANNOT execute regardless of who clicks "approve". The
     founder must run them by hand.
  2. Approval action: only `action == 'approved'` proceeds. Reject and
     request_more_info short-circuit with no side-effect.
  3. Per-function rate limit: at most one applied fix per function in a
     7-day window (per the panel rate-limit recommendation). Override
     only via `DELIMIT_SELF_REPAIR_OVERRIDE_RATE=1` env var — no config
     toggle, so the override leaves a process-tree breadcrumb if it is
     ever used.

Each fix tier maps to a small executor:

  - prompt_rewrite: writes `proposed_fix.specifics.new_prompt` to
    `proposed_fix.specifics.prompt_path` and commits with a
    `[self-repair]` tag so the change is reversible by `git revert`.
  - kpi_adjust:    writes a one-shot threshold override to
    `~/.delimit/self_repair_overrides/<function>.yaml`. Permanent
    yaml edits remain manual.
  - disable_temp:  flips `pause: true` on the function in the
    self-repair config and records an unpause time
    (`disable_temp_until` ISO timestamp). The watcher's
    `unpause_expired_disables` reverses this when due.
  - code_change | scope_expansion | spend_increase: REFUSE auto-apply.
    These tiers always require a manual code/PR/billing change. The
    apply gate logs the proposed fix and emails the founder with the
    diff and a "click to PR" pointer when possible.

Public API:
    apply_fix(history_record, founder_approval) -> FixOutcome
    unpause_expired_disables(now=None) -> list[str]
    write_kpi_override(function, kpi_name, threshold, *, ...) -> Path
    write_disable_pause(function, until, *, ...) -> Path

Constraints honored:
  - No new external deps (stdlib + already-imported PyYAML).
  - All on-disk writes use atomic tmp+rename via `_atomic_write_*`.
  - Every git commit produced here carries `[self-repair]` in the
    message so the change is auditable and revertable.
  - Returns `FixOutcome` always — exceptions only escape on disk
    corruption that callers cannot meaningfully recover from.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

from .history import (
    DEFAULT_HISTORY_PATH,
    iter_history,
    update_decision,
)
from .kpi import parse_window
from .mode import USER_CONFIG_PATH, _read_yaml

logger = logging.getLogger("delimit.ai.self_repair.apply")


# ── filesystem layout ────────────────────────────────────────────────


USER_DELIMIT_DIR = Path.home() / ".delimit"
DEFAULT_OVERRIDES_DIR = USER_DELIMIT_DIR / "self_repair_overrides"

# Auto-applied-fix log; one JSONL row per `apply_fix` call (success or
# refused). Used by the rate-limit gate and by `delimit self-repair
# history`. Distinct from `self_repair_history.jsonl` (which is the
# deliberation transcript log) — this file is the *outcome* log.
DEFAULT_APPLY_LOG_PATH = USER_DELIMIT_DIR / "self_repair_apply.jsonl"

# Per-tier behavior knob: how long disable_temp pauses by default.
DEFAULT_DISABLE_HOURS = 24


# ── data model ───────────────────────────────────────────────────────


@dataclass
class FixOutcome:
    """Outcome of one `apply_fix` invocation.

    `applied=False` with `error=...` covers every refusal path
    (escalation, non-approve action, rate-limit, tier refuses,
    disk failure inside an executor). `applied=True` is reserved for
    fixes whose tier executor returned success AND the apply log was
    written.

    `rollback_token` is a short opaque ID (uuid4 hex) callers can use
    later to look up an apply-log row and undo via the recorded
    `artifact_paths` (e.g. `git revert <commit>`). It is NOT the
    git commit SHA — git mutations may produce zero or more commits
    per fix (for instance kpi_adjust never commits).
    """

    history_id: str
    fix_tier: str
    applied: bool
    error: Optional[str]
    artifact_paths: List[str]
    rollback_token: str
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── helpers ──────────────────────────────────────────────────────────


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


def _atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    if _yaml is None:
        raise RuntimeError("PyYAML required for yaml writes")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.safe_dump(data, f, sort_keys=False)
    tmp.replace(path)


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def _iter_apply_log(
    *, log_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Read every row in the apply log, oldest first. Empty if missing."""
    target = log_path or DEFAULT_APPLY_LOG_PATH
    out: List[Dict[str, Any]] = []
    if not target.exists():
        return out
    with open(target, "r", encoding="utf-8") as f:
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


def _find_history_record(
    history_id: str, *, history_path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Look up a history row by `action_id` (the history_id in this layer's
    vocabulary — same value, different name to keep the apply API
    explicit about what it operates on)."""
    for row in iter_history(history_path=history_path):
        if row.get("action_id") == history_id:
            return row
    return None


# ── rate-limit gate ──────────────────────────────────────────────────


def _count_recent_applied(
    function: str,
    *,
    now: datetime,
    log_path: Optional[Path] = None,
    days: int = 7,
) -> int:
    """Count successful `apply_fix` invocations for `function` in the
    last `days`. Refused / errored rows do NOT count toward the rate
    limit — refusing 5 fixes does not block legitimate ones."""
    target = log_path or DEFAULT_APPLY_LOG_PATH
    cutoff = now - timedelta(days=days)
    n = 0
    for row in _iter_apply_log(log_path=target):
        if row.get("function") != function:
            continue
        if not row.get("applied"):
            continue
        ts_raw = row.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        n += 1
    return n


# ── per-tier executors ───────────────────────────────────────────────


def _git_commit(
    repo_dir: Path,
    paths: List[Path],
    *,
    message: str,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Stage `paths` and create a commit in `repo_dir`.

    Returns (ok, commit_sha, error). Best-effort: if `repo_dir` is not
    a git repo, returns (False, None, "not a git repo"). The caller
    decides whether that should fail the apply or just be logged.

    The commit message is forced to begin with `[self-repair]` so the
    audit trail is greppable; callers should pass the full intended
    message and we prepend the tag if missing.
    """
    if not message.startswith("[self-repair]"):
        message = f"[self-repair] {message}"
    try:
        rel = [str(p.relative_to(repo_dir)) for p in paths]
    except ValueError:
        return False, None, f"path outside repo: {[str(p) for p in paths]}"

    # Add the files. We don't `git add -A` to avoid sweeping unrelated
    # working-tree dirt into the commit (Customer Protection rule).
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "--"] + rel,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, None, "git CLI not found"
    except subprocess.CalledProcessError as exc:
        return False, None, f"git add failed: {exc.stderr.strip()[:200]}"

    # If nothing staged, skip the commit cleanly.
    diff_proc = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not diff_proc.stdout.strip():
        return True, None, "nothing to commit"

    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        return False, None, f"git commit failed: {exc.stderr.strip()[:200]}"

    sha_proc = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    sha = sha_proc.stdout.strip() or None
    return True, sha, None


def _find_repo_root(path: Path) -> Optional[Path]:
    """Walk up from `path` looking for a `.git` directory. Returns None
    if none found before hitting the filesystem root."""
    cur = path.resolve()
    if cur.is_file():
        cur = cur.parent
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def _apply_prompt_rewrite(
    *,
    record: Dict[str, Any],
    specifics: Dict[str, Any],
    history_id: str,
    rollback_token: str,
    now: datetime,
) -> Tuple[bool, Optional[str], List[str]]:
    """Apply a prompt_rewrite tier fix.

    Required `specifics`:
      - `prompt_path`: absolute path to the prompt file.
      - `new_prompt`: the new content (string). Replaces the file in full.

    Optional:
      - `commit`: bool, default True. If False, file is written but
        no git commit is made (used in tests + for prompts living
        outside a git repo).

    Returns (ok, error, artifact_paths).
    """
    prompt_path_raw = specifics.get("prompt_path")
    new_prompt = specifics.get("new_prompt")
    if not prompt_path_raw or not isinstance(new_prompt, str):
        return (
            False,
            "prompt_path_invalid",
            [],
        )
    prompt_path = Path(str(prompt_path_raw)).expanduser()
    if not prompt_path.exists():
        return False, "prompt_path_invalid", []

    try:
        _atomic_write_text(prompt_path, new_prompt)
    except OSError as exc:
        return False, f"prompt write failed: {exc}", []

    artifact_paths = [str(prompt_path)]
    if specifics.get("commit", True):
        repo = _find_repo_root(prompt_path)
        if repo is not None:
            fn = record.get("function") or "unknown"
            kpi = record.get("breach_kpi") or "unknown"
            ok, sha, git_err = _git_commit(
                repo,
                [prompt_path],
                message=(
                    f"approved fix for {fn} :: {kpi} "
                    f"(history_id={history_id}, rollback_token={rollback_token})"
                ),
            )
            if not ok:
                # We've already written the file; surface the git error
                # but keep the artifact path so the operator can manually
                # commit if they want a git trail.
                logger.warning(
                    "self_repair: git commit failed for %s: %s",
                    history_id,
                    git_err,
                )
            elif sha:
                artifact_paths.append(f"git:{sha}")
        else:
            logger.info(
                "self_repair: prompt %s not in a git repo; skipping commit",
                prompt_path,
            )
    return True, None, artifact_paths


def write_kpi_override(
    function: str,
    kpi_name: str,
    threshold: float,
    *,
    overrides_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    rollback_token: Optional[str] = None,
    severity_kind: Optional[str] = None,
) -> Path:
    """Write a one-shot KPI threshold override.

    Layout: `<overrides_dir>/<function>.yaml`. Format:

        version: 1
        function: marketing.social_outreach
        kpi_overrides:
          drafts_per_day:
            threshold: 3
            kind: floor   # or ceiling
            applied_at: 2026-04-30T...
            rollback_token: deadbeef

    Permanent KPI changes still require manual yaml edit. The override
    file is read by callers that want to know about ad-hoc adjustments
    (e.g. a future `evaluate_kpi` extension that respects them).
    """
    target_dir = overrides_dir or DEFAULT_OVERRIDES_DIR
    safe_fn = function.replace("/", "_").replace("..", "_")
    target = target_dir / f"{safe_fn}.yaml"

    existing: Dict[str, Any] = {}
    if target.exists():
        existing = _read_yaml(target) or {}

    existing.setdefault("version", 1)
    existing["function"] = function
    overrides = existing.setdefault("kpi_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
        existing["kpi_overrides"] = overrides
    entry: Dict[str, Any] = {
        "threshold": threshold,
        "applied_at": _now(now).isoformat(),
    }
    if rollback_token:
        entry["rollback_token"] = rollback_token
    if severity_kind:
        entry["kind"] = severity_kind
    overrides[kpi_name] = entry

    _atomic_write_yaml(target, existing)
    return target


def _apply_kpi_adjust(
    *,
    record: Dict[str, Any],
    specifics: Dict[str, Any],
    history_id: str,
    rollback_token: str,
    now: datetime,
    overrides_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Apply a kpi_adjust tier fix.

    `specifics` shape (any of these is sufficient):
      - new_threshold (float): the new floor or ceiling.
      - kind ('floor' | 'ceiling'): optional; falls back to inferring
        from the original record's threshold/actual relationship.

    Falls back to the record's existing threshold if `new_threshold`
    is missing.
    """
    function = str(record.get("function") or "unknown")
    kpi = str(record.get("breach_kpi") or "unknown")
    new_threshold = specifics.get("new_threshold")
    if new_threshold is None:
        new_threshold = specifics.get("threshold")
    if new_threshold is None:
        return False, "kpi_adjust_missing_threshold", []
    try:
        new_threshold = float(new_threshold)
    except (TypeError, ValueError):
        return False, "kpi_adjust_invalid_threshold", []

    kind = specifics.get("kind")
    if kind not in ("floor", "ceiling"):
        # Best-guess from the original breach: actual < threshold means
        # we crossed below a floor; actual > threshold means we crossed
        # above a ceiling.
        actual = record.get("actual")
        thr = record.get("threshold")
        if isinstance(actual, (int, float)) and isinstance(thr, (int, float)):
            kind = "floor" if actual < thr else "ceiling"

    target = write_kpi_override(
        function,
        kpi,
        new_threshold,
        overrides_dir=overrides_dir,
        now=now,
        rollback_token=rollback_token,
        severity_kind=kind,
    )
    return True, None, [str(target)]


def write_disable_pause(
    function: str,
    until: datetime,
    *,
    config_path: Optional[Path] = None,
    rollback_token: Optional[str] = None,
) -> Path:
    """Set `pause: true` on `function` in the self-repair config and
    record `disable_temp_until` so the watcher can reverse it later.

    Returns the path to the config file that was written.
    """
    target = config_path or USER_CONFIG_PATH
    if _yaml is None:
        raise RuntimeError("PyYAML required for disable_temp")

    base = _read_yaml(target) if target.exists() else {}
    if not base:
        base = {"version": 1, "default_mode": "off", "pause": False, "functions": {}}

    functions = base.setdefault("functions", {})
    if not isinstance(functions, dict):
        functions = {}
        base["functions"] = functions
    fn_cfg = functions.setdefault(function, {})
    if not isinstance(fn_cfg, dict):
        fn_cfg = {}
        functions[function] = fn_cfg

    fn_cfg["pause"] = True
    fn_cfg["disable_temp_until"] = until.isoformat()
    if rollback_token:
        fn_cfg["disable_temp_rollback_token"] = rollback_token

    _atomic_write_yaml(target, base)
    return target


def _apply_disable_temp(
    *,
    record: Dict[str, Any],
    specifics: Dict[str, Any],
    history_id: str,
    rollback_token: str,
    now: datetime,
    config_path: Optional[Path] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Apply a disable_temp tier fix.

    `specifics.duration_hours` (default 24) controls how long the pause
    lasts. The watcher's `unpause_expired_disables` flips pause back to
    false once `disable_temp_until` is in the past.
    """
    function = str(record.get("function") or "unknown")
    duration_hours_raw = specifics.get("duration_hours", DEFAULT_DISABLE_HOURS)
    try:
        duration_hours = float(duration_hours_raw)
    except (TypeError, ValueError):
        duration_hours = DEFAULT_DISABLE_HOURS
    if duration_hours <= 0:
        duration_hours = DEFAULT_DISABLE_HOURS

    until = now + timedelta(hours=duration_hours)
    try:
        target = write_disable_pause(
            function,
            until,
            config_path=config_path,
            rollback_token=rollback_token,
        )
    except RuntimeError as exc:
        return False, str(exc), []
    return True, None, [str(target)]


def _refuse_manual(tier: str) -> Tuple[bool, str, List[str]]:
    """Tiers that we never auto-apply, even with founder approval."""
    return False, f"{tier}_requires_manual_apply", []


# ── public entry-point ──────────────────────────────────────────────


def apply_fix(
    history_record: Dict[str, Any],
    founder_approval: Dict[str, Any],
    *,
    history_path: Optional[Path] = None,
    apply_log_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    overrides_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> FixOutcome:
    """Gate, then dispatch, an approved self-repair fix.

    `history_record` is the canonical row from `self_repair_history.jsonl`
    (the same dict shape produced by `history._build_record`).

    `founder_approval` is a small dict from the inbox executor:
        {"action": "approved" | "rejected" | "more_info",
         "reply_text": <body>}
    Only `action == "approved"` is allowed to apply. Other actions are
    refused with a corresponding error string and update the history
    record's `founder_decision` accordingly.

    Returns a `FixOutcome` describing what (if anything) happened. The
    apply log row is appended on every call (including refusals) so
    the audit trail captures rate-limit denials and escalation refusals
    as well as successes.
    """
    anchor = _now(now)
    timestamp = anchor.isoformat()
    rollback_token = uuid.uuid4().hex[:16]

    history_id = str(history_record.get("action_id") or "")
    fix_tier = str(history_record.get("fix_tier") or "code_change")
    function = str(history_record.get("function") or "unknown")

    # Gate 0: we must have an action_id to correlate to history.
    if not history_id:
        outcome = FixOutcome(
            history_id="",
            fix_tier=fix_tier,
            applied=False,
            error="history_record_missing_action_id",
            artifact_paths=[],
            rollback_token=rollback_token,
            timestamp=timestamp,
        )
        _log_apply(outcome, function=function, log_path=apply_log_path)
        return outcome

    action = str((founder_approval or {}).get("action") or "").strip().lower()

    # Gate 1: escalation hard-stops cannot be bypassed by founder approval.
    # The constitution is the constitution — even a green light from the
    # founder requires manual execution outside this loop.
    escalation_class = history_record.get("escalation_class") or []
    if escalation_class:
        outcome = FixOutcome(
            history_id=history_id,
            fix_tier=fix_tier,
            applied=False,
            error="escalation_class_hard_stop_refused",
            artifact_paths=[],
            rollback_token=rollback_token,
            timestamp=timestamp,
        )
        # Mark the history row so a re-run of apply doesn't even look
        # at it again.
        update_decision(
            history_id,
            "refused_escalation",
            history_path=history_path,
        )
        _log_apply(
            outcome,
            function=function,
            log_path=apply_log_path,
            extra={"escalation_class": list(escalation_class)},
        )
        return outcome

    # Gate 2: only 'approved' applies.
    if action != "approved":
        # Map non-approve actions to history decisions for the audit.
        if action == "rejected":
            decision = "rejected"
            error = "founder_rejected"
        elif action in ("more_info", "info", "request_more_info"):
            decision = "more_info"
            error = "founder_requested_more_info"
        else:
            decision = "no_action"
            error = f"unknown_founder_action:{action or 'missing'}"
        update_decision(history_id, decision, history_path=history_path)
        outcome = FixOutcome(
            history_id=history_id,
            fix_tier=fix_tier,
            applied=False,
            error=error,
            artifact_paths=[],
            rollback_token=rollback_token,
            timestamp=timestamp,
        )
        _log_apply(outcome, function=function, log_path=apply_log_path)
        return outcome

    # Gate 3: rate limit (1 fix / function / 7d). Override env wins.
    override_rate = (
        os.environ.get("DELIMIT_SELF_REPAIR_OVERRIDE_RATE", "").strip() == "1"
    )
    if not override_rate:
        recent = _count_recent_applied(
            function, now=anchor, log_path=apply_log_path
        )
        if recent >= 1:
            outcome = FixOutcome(
                history_id=history_id,
                fix_tier=fix_tier,
                applied=False,
                error="rate_limit_exceeded",
                artifact_paths=[],
                rollback_token=rollback_token,
                timestamp=timestamp,
            )
            update_decision(
                history_id,
                "rate_limited",
                history_path=history_path,
            )
            _log_apply(
                outcome,
                function=function,
                log_path=apply_log_path,
                extra={"recent_applied": recent},
            )
            return outcome

    # Per-tier dispatch.
    proposed_fix = history_record.get("proposed_fix") or {}
    if not isinstance(proposed_fix, dict):
        proposed_fix = {}
    specifics = proposed_fix.get("specifics") or {}
    if not isinstance(specifics, dict):
        specifics = {}

    ok: bool
    err: Optional[str]
    artifacts: List[str]

    if fix_tier == "prompt_rewrite":
        ok, err, artifacts = _apply_prompt_rewrite(
            record=history_record,
            specifics=specifics,
            history_id=history_id,
            rollback_token=rollback_token,
            now=anchor,
        )
    elif fix_tier == "kpi_adjust":
        ok, err, artifacts = _apply_kpi_adjust(
            record=history_record,
            specifics=specifics,
            history_id=history_id,
            rollback_token=rollback_token,
            now=anchor,
            overrides_dir=overrides_dir,
        )
    elif fix_tier == "disable_temp":
        ok, err, artifacts = _apply_disable_temp(
            record=history_record,
            specifics=specifics,
            history_id=history_id,
            rollback_token=rollback_token,
            now=anchor,
            config_path=config_path,
        )
    elif fix_tier in ("code_change", "scope_expansion", "spend_increase"):
        ok, err, artifacts = _refuse_manual(fix_tier)
    else:
        ok = False
        err = f"unknown_tier:{fix_tier}"
        artifacts = []

    outcome = FixOutcome(
        history_id=history_id,
        fix_tier=fix_tier,
        applied=ok,
        error=err,
        artifact_paths=artifacts,
        rollback_token=rollback_token,
        timestamp=timestamp,
    )

    if ok:
        update_decision(history_id, "applied", history_path=history_path)
    else:
        # Tier-level refusals (manual-only) get a distinct decision so
        # the founder sees why the loop didn't act despite approval.
        if err and err.endswith("_requires_manual_apply"):
            update_decision(
                history_id,
                "approved_manual_required",
                history_path=history_path,
            )
        else:
            update_decision(
                history_id,
                f"apply_failed:{err or 'unknown'}",
                history_path=history_path,
            )

    _log_apply(
        outcome,
        function=function,
        log_path=apply_log_path,
        extra={"specifics_keys": sorted(specifics.keys())},
    )
    return outcome


# ── apply log writer ────────────────────────────────────────────────


def _log_apply(
    outcome: FixOutcome,
    *,
    function: str,
    log_path: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one row to the apply log. Best-effort."""
    target = log_path or DEFAULT_APPLY_LOG_PATH
    row: Dict[str, Any] = {
        "history_id": outcome.history_id,
        "function": function,
        "fix_tier": outcome.fix_tier,
        "applied": outcome.applied,
        "error": outcome.error,
        "artifact_paths": list(outcome.artifact_paths),
        "rollback_token": outcome.rollback_token,
        "timestamp": outcome.timestamp,
    }
    if extra:
        row.update(extra)
    try:
        _append_jsonl(target, row)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("self_repair: apply-log write failed: %s", exc)


# ── unpause helper (called by the watcher) ──────────────────────────


def unpause_expired_disables(
    now: Optional[datetime] = None,
    *,
    config_path: Optional[Path] = None,
) -> List[str]:
    """Iterate the self-repair config, find functions with
    `disable_temp_until` in the past, and flip their pause flag back to
    false. Returns the list of function names that were unpaused.

    Idempotent: a function whose `disable_temp_until` has already been
    cleared is ignored. Tolerates a missing config file (returns []).
    """
    anchor = _now(now)
    target = config_path or USER_CONFIG_PATH
    if not target.exists() or _yaml is None:
        return []
    base = _read_yaml(target) or {}
    functions = base.get("functions") or {}
    if not isinstance(functions, dict):
        return []

    unpaused: List[str] = []
    dirty = False
    for fn, cfg in functions.items():
        if not isinstance(cfg, dict):
            continue
        until_raw = cfg.get("disable_temp_until")
        if not isinstance(until_raw, str):
            continue
        try:
            until = datetime.fromisoformat(until_raw.rstrip("Z"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if until <= anchor:
            cfg["pause"] = False
            cfg.pop("disable_temp_until", None)
            cfg.pop("disable_temp_rollback_token", None)
            unpaused.append(fn)
            dirty = True

    if dirty:
        try:
            _atomic_write_yaml(target, base)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                "self_repair: failed to persist unpause for %s: %s",
                unpaused,
                exc,
            )
            return []
        logger.info("self_repair: unpaused expired disables: %s", unpaused)
    return unpaused


# Convenience: load + apply via history_id alone (used by the CLI).


def apply_by_history_id(
    history_id: str,
    *,
    history_path: Optional[Path] = None,
    apply_log_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    overrides_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    reply_text: str = "(manual cli apply)",
) -> FixOutcome:
    """Look up the history record by `history_id` and run `apply_fix`
    with a synthetic founder_approval. Used by `delimit self-repair
    apply <history_id>` for cases where the inbox executor missed an
    approval reply (e.g. founder forwards instead of replies)."""
    record = _find_history_record(history_id, history_path=history_path)
    if record is None:
        return FixOutcome(
            history_id=history_id,
            fix_tier="unknown",
            applied=False,
            error="history_record_not_found",
            artifact_paths=[],
            rollback_token=uuid.uuid4().hex[:16],
            timestamp=_now(now).isoformat(),
        )
    return apply_fix(
        record,
        {"action": "approved", "reply_text": reply_text},
        history_path=history_path,
        apply_log_path=apply_log_path,
        config_path=config_path,
        overrides_dir=overrides_dir,
        now=now,
    )
