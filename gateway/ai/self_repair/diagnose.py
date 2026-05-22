"""
Self-repair diagnostic evidence gatherer (diagnose-mode).

Given a `Breach` (from the alert-mode watcher) and the function's KPI yaml,
this module gathers a 30-day evidence bundle for founder review. It does
NOT call deliberate, NOT propose a fix, and NOT touch any state outside
the breach's source files.

The bundle is designed to answer four questions for the founder:

  1. What happened? — `summary`, the originating breach
  2. What's been going on? — `activity_30d`, breakdowns by day / platform /
     venture / status etc., depending on the function's declared sources
  3. Is this new? — `trend` (last 7d vs prior 7d vs prior 30d) and
     `baseline` (all-time mean / median, last-known-good window)
  4. Is anything else broken at the same time? — `related_signals`, pulled
     from `~/.delimit/audit/`, `~/.delimit/cron.log`, recent ledger items
     tagged with the function name, and the watcher's own state file (so
     a cross-function correlation shows up)

Public API:
    gather_diagnostic(breach, function_yaml) -> DiagnosticBundle
    render_text(bundle) -> str
    render_json(bundle) -> dict

Constraints honored here:
  - Pure stdlib (no new external deps)
  - No writes to `~/.delimit/` (read-only)
  - Tolerates missing data sources — empty bundles still render
  - If gathering fails, callers (e.g., the watcher) should fall through
    to alert-mode behavior; this module does not retry on its own
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .kpi import Breach, _iter_jsonl, _record_ts

logger = logging.getLogger("delimit.ai.self_repair.diagnose")

# Default search roots (overridable by the caller / tests).
USER_DELIMIT_DIR = Path.home() / ".delimit"
USER_AUDIT_DIR = USER_DELIMIT_DIR / "audit"
USER_CRON_LOG = USER_DELIMIT_DIR / "cron.log"
USER_LEDGER_DIR = USER_DELIMIT_DIR / "ledger"
USER_STATE_PATH = USER_DELIMIT_DIR / "self_repair_state.json"

# Window over which we gather raw activity. The breach itself is what
# triggered the bundle, so we always look back from the breach timestamp.
DEFAULT_WINDOW_DAYS = 30


# ── data model ───────────────────────────────────────────────────────


@dataclass
class DiagnosticBundle:
    """A 30-day evidence bundle keyed to a single breach.

    All numeric fields are JSON-serializable. `breach` is preserved as
    a `Breach` dataclass; `render_json` converts it to a dict.
    """

    breach: Breach
    function: str
    timestamp: str  # ISO8601 UTC — when the bundle was produced
    summary: str  # one-line human-readable summary
    activity_30d: Dict[str, Any] = field(default_factory=dict)
    trend: Dict[str, Any] = field(default_factory=dict)
    baseline: Dict[str, Any] = field(default_factory=dict)
    related_signals: List[Dict[str, Any]] = field(default_factory=list)
    evidence_paths: List[str] = field(default_factory=list)


# ── source resolution ────────────────────────────────────────────────


def _resolve_sources(
    function_yaml: Dict[str, Any],
    *,
    sources_base_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """Convert the yaml `sources:` block into a {name: Path} map.

    Mirrors `kpi._resolve_sources` but is duplicated here so the
    diagnose module can be imported without coupling to the kpi
    module's private helpers. Sources whose value is None are dropped.
    """
    sources_block = function_yaml.get("sources") or {}
    out: Dict[str, Path] = {}
    if not isinstance(sources_block, dict):
        return out

    def _resolve(raw: Any) -> Optional[Path]:
        if raw is None:
            return None
        s = str(raw)
        if sources_base_dir is not None and s.startswith("~/.delimit/"):
            tail = s[len("~/.delimit/") :]
            return Path(sources_base_dir) / tail
        return Path(s).expanduser()

    for key, val in sources_block.items():
        if key == "external":
            if isinstance(val, dict):
                for ext_key, ext_val in val.items():
                    p = _resolve(ext_val)
                    if p is not None:
                        out[ext_key] = p
            continue
        p = _resolve(val)
        if p is not None:
            out[key] = p
    return out


# ── breach-timestamp resolution ──────────────────────────────────────


def _breach_now(breach: Breach, *, now: Optional[datetime] = None) -> datetime:
    """Resolve the reference time for the bundle.

    Caller may pass an explicit `now`; otherwise we use the current UTC
    time. We do NOT attempt to recover a timestamp from the Breach
    dataclass itself (it doesn't carry one) — the watcher detects "now"
    and fires the breach immediately, so wall-clock now is the right
    anchor.
    """
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now
    return datetime.now(tz=timezone.utc)


# ── activity gathering ───────────────────────────────────────────────


def _records_within(
    path: Path, *, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    """Return records whose `ts` is in [since, until]. Skips bad lines.

    Uses `_iter_jsonl` and `_record_ts` from `kpi.py` so behaviour is
    identical to the evaluator (same tolerance for trailing 'Z',
    int/float epochs, missing tz, etc.).
    """
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for r in _iter_jsonl(path):
        ts = _record_ts(r)
        if ts is None:
            continue
        if since <= ts <= until:
            out.append(r)
    return out


def _by_day(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Group records by `YYYY-MM-DD` of their `ts`."""
    counter: Counter[str] = Counter()
    for r in records:
        ts = _record_ts(r)
        if ts is None:
            continue
        counter[ts.date().isoformat()] += 1
    return dict(sorted(counter.items()))


def _top_values(
    records: List[Dict[str, Any]], field_name: str, *, top: int = 5
) -> List[Tuple[str, int]]:
    """Return the top `top` values for `field_name` as (value, count) pairs."""
    counter: Counter[str] = Counter()
    for r in records:
        v = r.get(field_name)
        if v is None:
            continue
        counter[str(v)] += 1
    return counter.most_common(top)


def _activity_for_source(
    source_name: str,
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a generic activity summary for one source's records.

    Per-source customization (e.g., social drafts get a quality
    distribution) is layered on top by `gather_diagnostic`.
    """
    out: Dict[str, Any] = {
        "total": len(records),
        "by_day": _by_day(records),
    }

    # Heuristic: surface field breakdowns that the social_outreach KPIs
    # already care about, when present.
    for fld in ("platform", "venture", "status", "spam_flag", "target_id"):
        present = any(fld in r for r in records)
        if not present:
            continue
        out[f"top_{fld}"] = [
            {"value": v, "count": c}
            for v, c in _top_values(records, fld, top=10)
        ]

    return out


def _social_drafts_extras(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Social-outreach-specific: drafts breakdown by platform + venue."""
    extras: Dict[str, Any] = {}
    if not records:
        return extras

    drafts_per_platform = Counter(
        str(r.get("platform")) for r in records if r.get("platform") is not None
    )
    if drafts_per_platform:
        extras["drafts_per_platform"] = dict(drafts_per_platform.most_common())

    # Top venues = most frequently drafted target_ids; also surfaced
    # so duplicate_target_rate breaches are easy to interpret.
    drafts_per_target = Counter(
        str(r.get("target_id"))
        for r in records
        if r.get("target_id") is not None
    )
    if drafts_per_target:
        extras["top_target_ids"] = [
            {"target_id": tid, "count": c}
            for tid, c in drafts_per_target.most_common(10)
        ]

    # Quality distribution if records carry a `quality` field.
    qualities = [r.get("quality") for r in records if r.get("quality") is not None]
    if qualities:
        qcounter = Counter(str(q) for q in qualities)
        extras["quality_distribution"] = dict(qcounter.most_common())

    return extras


def _notify_success_rate(
    notifications: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Compute notify success/failure rate from the notifications source.

    Tolerates absent fields. Looks at `status` (preferred) then `success`
    (boolean). Returns None if neither is usable.
    """
    if not notifications:
        return None
    statuses = [r.get("status") for r in notifications if r.get("status") is not None]
    if statuses:
        c = Counter(str(s).lower() for s in statuses)
        ok = c.get("ok", 0) + c.get("sent", 0) + c.get("success", 0)
        fail = c.get("fail", 0) + c.get("failed", 0) + c.get("error", 0)
        total = sum(c.values())
        if total == 0:
            return None
        return {
            "total": total,
            "ok": ok,
            "fail": fail,
            "by_status": dict(c.most_common()),
            "success_rate": round(ok / total, 4) if total else None,
        }
    successes = [r.get("success") for r in notifications if "success" in r]
    if successes:
        ok = sum(1 for s in successes if bool(s))
        fail = len(successes) - ok
        return {
            "total": len(successes),
            "ok": ok,
            "fail": fail,
            "success_rate": round(ok / len(successes), 4) if successes else None,
        }
    return None


# ── trend / baseline ─────────────────────────────────────────────────


def _per_day_count(
    records: List[Dict[str, Any]], *, since: datetime, until: datetime
) -> float:
    """Drafts (or any record) per day across [since, until]."""
    days = max(1.0, (until - since).total_seconds() / 86400.0)
    return round(len(records) / days, 4)


def _approval_rate(records: List[Dict[str, Any]]) -> Optional[float]:
    """Founder-approval rate — fraction of owner_actions records with
    `status='approved'`. Returns None if the source has no usable data.
    """
    if not records:
        return None
    seen = [r for r in records if r.get("status") is not None]
    if not seen:
        return None
    approved = sum(1 for r in seen if str(r.get("status")).lower() == "approved")
    return round(approved / len(seen), 4)


def _diversity_count(records: List[Dict[str, Any]], field_name: str) -> int:
    """Number of distinct values of `field_name` across `records`."""
    return len(
        {r.get(field_name) for r in records if r.get(field_name) is not None}
    )


def _gather_trend(
    sources: Dict[str, Path],
    *,
    now: datetime,
) -> Dict[str, Any]:
    """Compare last 7d vs prior 7d vs prior 30d windows."""
    win_now = now - timedelta(days=7)
    win_prior = now - timedelta(days=14)
    win_30 = now - timedelta(days=30)

    def _bucket(path: Path, lo: datetime, hi: datetime) -> List[Dict[str, Any]]:
        return _records_within(path, since=lo, until=hi)

    trend: Dict[str, Any] = {}

    drafts_path = sources.get("drafts")
    if drafts_path is not None:
        last_7 = _bucket(drafts_path, win_now, now)
        prior_7 = _bucket(drafts_path, win_prior, win_now)
        prior_30 = _bucket(drafts_path, win_30, win_prior)

        trend["drafts_per_day_last_7d"] = _per_day_count(
            last_7, since=win_now, until=now
        )
        trend["drafts_per_day_prior_7d"] = _per_day_count(
            prior_7, since=win_prior, until=win_now
        )
        trend["drafts_per_day_prior_30d"] = _per_day_count(
            prior_30, since=win_30, until=win_prior
        )

        # Diversity delta (platforms covered in each window).
        trend["platform_diversity_last_7d"] = _diversity_count(
            last_7, "platform"
        )
        trend["platform_diversity_prior_7d"] = _diversity_count(
            prior_7, "platform"
        )

        # Throughput delta as a simple ratio.
        prior_rate = trend["drafts_per_day_prior_7d"]
        if prior_rate:
            trend["throughput_delta_pct"] = round(
                100.0 * (trend["drafts_per_day_last_7d"] - prior_rate) / prior_rate,
                2,
            )
        else:
            trend["throughput_delta_pct"] = None

    actions_path = sources.get("owner_actions")
    if actions_path is not None:
        last_7 = _bucket(actions_path, win_now, now)
        prior_7 = _bucket(actions_path, win_prior, win_now)
        ar_now = _approval_rate(last_7)
        ar_prior = _approval_rate(prior_7)
        trend["approval_rate_last_7d"] = ar_now
        trend["approval_rate_prior_7d"] = ar_prior
        if ar_now is not None and ar_prior is not None:
            trend["approval_rate_delta"] = round(ar_now - ar_prior, 4)

    return trend


def _gather_baseline(
    sources: Dict[str, Path],
    *,
    now: datetime,
    breach: Breach,
) -> Dict[str, Any]:
    """All-time mean/median + last-known-good 7d window for breach KPI.

    Best-effort: covers the metrics referenced in the social_outreach
    pilot (drafts/day, approval rate). Functions whose source data does
    not match these keys get a minimal baseline.
    """
    baseline: Dict[str, Any] = {}

    drafts_path = sources.get("drafts")
    if drafts_path is not None and drafts_path.exists():
        # All-time: per-day counts -> mean / median.
        all_records = list(_iter_jsonl(drafts_path))
        per_day: Counter[str] = Counter()
        for r in all_records:
            ts = _record_ts(r)
            if ts is None:
                continue
            per_day[ts.date().isoformat()] += 1

        if per_day:
            counts = list(per_day.values())
            baseline["drafts_per_day_alltime_mean"] = round(
                statistics.fmean(counts), 4
            )
            baseline["drafts_per_day_alltime_median"] = float(
                statistics.median(counts)
            )
            baseline["drafts_per_day_alltime_max"] = max(counts)
            baseline["alltime_days_with_data"] = len(per_day)
        else:
            baseline["drafts_per_day_alltime_mean"] = 0.0
            baseline["alltime_days_with_data"] = 0

        # Last-known-good 7d window for the drafts_per_day floor (5/day).
        # We slide a 7d window backwards from `now - 7d` until we find
        # one where average drafts/day >= 5 (the bundled floor).
        floor = breach.threshold if breach.threshold is not None else 5.0
        kpi = (breach.kpi_name or "").lower()
        if "drafts_per_day" in kpi or "drafts_per_week" in kpi:
            lkg = _last_known_good(
                all_records,
                now=now,
                window=timedelta(days=7),
                # Floor is per-day-rate; if the breach was per-week,
                # convert to per-day basis.
                floor=floor / (7.0 if "per_week" in kpi else 1.0),
            )
            if lkg is not None:
                baseline["last_known_good_window"] = lkg

    actions_path = sources.get("owner_actions")
    if actions_path is not None and actions_path.exists():
        all_actions = list(_iter_jsonl(actions_path))
        ar = _approval_rate(all_actions)
        if ar is not None:
            baseline["approval_rate_alltime"] = ar
            baseline["approval_rate_alltime_n"] = len(all_actions)

    return baseline


def _last_known_good(
    records: List[Dict[str, Any]],
    *,
    now: datetime,
    window: timedelta,
    floor: float,
) -> Optional[Dict[str, Any]]:
    """Find the most recent 7d window whose drafts/day rate >= floor.

    Slides backwards in 1-day steps starting from `now - window`. Returns
    `{start, end, rate}` for the first hit. None if no window passes.
    Bounded at 365 days to keep cost predictable.
    """
    # Bucket records by date once.
    by_date: Counter[str] = Counter()
    for r in records:
        ts = _record_ts(r)
        if ts is None:
            continue
        by_date[ts.date().isoformat()] += 1

    if not by_date:
        return None

    step = timedelta(days=1)
    end = now - window  # candidate window end == now - 7d
    horizon = now - timedelta(days=365)
    while end > horizon:
        start = end - window
        # Sum drafts in [start, end].
        total = 0
        d = start.date()
        while d <= end.date():
            total += by_date.get(d.isoformat(), 0)
            d = d.fromordinal(d.toordinal() + 1)
        days = max(1.0, (end - start).total_seconds() / 86400.0)
        rate = total / days
        if rate >= floor:
            return {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rate_per_day": round(rate, 4),
                "total_records": total,
            }
        end -= step
    return None


# ── related signals ──────────────────────────────────────────────────


def _scan_audit_dir(
    audit_dir: Path,
    *,
    function: str,
    since: datetime,
    until: datetime,
    max_events: int = 25,
) -> List[Dict[str, Any]]:
    """Pull recent audit events relevant to `function` from JSONL files
    in `audit_dir`. Each file is named `audit-YYYY-MM-DD.jsonl` per the
    existing convention; we filter to files whose date stamp is in the
    window, then to events that mention the function name (substring) or
    are flagged severity high/critical.

    Returns at most `max_events` event dicts, oldest first.
    """
    if not audit_dir.exists():
        return []

    short = function.split(".")[-1] if "." in function else function

    candidates: List[Path] = []
    for p in sorted(audit_dir.glob("*.jsonl")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        if not m:
            continue
        try:
            d = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # File covers a day; keep it if [since, until] could possibly
        # overlap that day.
        day_start = d
        day_end = d + timedelta(days=1)
        if day_end < since or day_start > until:
            continue
        candidates.append(p)

    out: List[Dict[str, Any]] = []
    for p in candidates:
        for r in _iter_jsonl(p):
            ts_raw = r.get("timestamp") or r.get("ts")
            ts: Optional[datetime] = None
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
            if ts is None or not (since <= ts <= until):
                continue

            blob = json.dumps(r).lower()
            severity = str(r.get("severity") or "").lower()
            mentions_fn = function.lower() in blob or short.lower() in blob
            is_high = severity in ("high", "critical", "p0", "p1")
            if not (mentions_fn or is_high):
                continue
            out.append(
                {
                    "source": str(p),
                    "ts": ts.isoformat(),
                    "event": r.get("event") or r.get("type") or "audit_event",
                    "severity": severity or "info",
                    "summary": _short_repr(r),
                }
            )
            if len(out) >= max_events:
                return out
    return out


def _short_repr(record: Dict[str, Any], *, max_len: int = 200) -> str:
    """Render a short single-line representation of an audit event."""
    keys_of_interest = ("event", "type", "tool", "message", "title", "kpi", "function")
    parts: List[str] = []
    for k in keys_of_interest:
        v = record.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    if not parts:
        s = json.dumps(record)
    else:
        s = " ".join(parts)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def _scan_cron_log(
    cron_path: Path,
    *,
    function: str,
    since: datetime,
    max_lines: int = 25,
) -> List[Dict[str, Any]]:
    """Scan tail of cron.log for lines mentioning `function` or `error`.

    cron.log can be huge. We cap reads at ~5MB tail and surface at most
    `max_lines` matches.
    """
    if not cron_path.exists():
        return []

    short = function.split(".")[-1] if "." in function else function
    needles = (
        function.lower(),
        short.lower(),
        "error",
        "traceback",
        "self_repair",
        "self-repair",
    )

    out: List[Dict[str, Any]] = []
    try:
        size = cron_path.stat().st_size
        max_bytes = 5 * 1024 * 1024  # 5 MB tail
        with open(cron_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial first line
            tail = f.read().decode("utf-8", errors="replace")
        for raw in tail.splitlines():
            low = raw.lower()
            if not any(n in low for n in needles):
                continue
            out.append({"source": str(cron_path), "line": raw[:300]})
            if len(out) >= max_lines:
                break
    except OSError as exc:
        logger.debug("self_repair: cannot scan cron.log: %s", exc)
    return out


def _scan_ledger(
    ledger_dir: Path,
    *,
    function: str,
    since: datetime,
    max_items: int = 15,
) -> List[Dict[str, Any]]:
    """Pull recent ledger items whose tags / title mention the function."""
    if not ledger_dir.exists():
        return []

    short = function.split(".")[-1] if "." in function else function
    out: List[Dict[str, Any]] = []
    for p in sorted(ledger_dir.glob("*.jsonl")):
        for r in _iter_jsonl(p):
            ts_raw = r.get("created_at") or r.get("ts")
            ts: Optional[datetime] = None
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
            if ts is None or ts < since:
                continue

            tags = r.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            blob = (
                str(r.get("title", ""))
                + " "
                + str(r.get("description", ""))
                + " "
                + " ".join(str(t) for t in tags)
            ).lower()
            if function.lower() not in blob and short.lower() not in blob:
                continue
            out.append(
                {
                    "source": str(p),
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "ts": ts.isoformat(),
                    "tags": tags,
                    "status": r.get("status"),
                }
            )
            if len(out) >= max_items:
                return out
    return out


def _scan_other_breaches(
    state_path: Path, *, function: str, since: datetime
) -> List[Dict[str, Any]]:
    """Read `self_repair_state.json` for breaches in OTHER functions in
    the recent window — surfaces cross-function correlation.
    """
    if not state_path.exists():
        return []
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(state, dict):
        return []

    out: List[Dict[str, Any]] = []
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        # state keys are formatted "<function>::<kpi>".
        fn = key.split("::", 1)[0] if "::" in key else key
        if fn == function:
            continue
        last = entry.get("last_fired_at")
        if not isinstance(last, str):
            continue
        try:
            last_dt = datetime.fromisoformat(last.rstrip("Z"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if last_dt < since:
            continue
        out.append(
            {
                "source": str(state_path),
                "function": fn,
                "kpi": key.split("::", 1)[1] if "::" in key else "",
                "ts": last_dt.isoformat(),
                "kind": "cross_function_breach",
            }
        )
    return out


def _gather_related_signals(
    *,
    function: str,
    now: datetime,
    audit_dir: Path,
    cron_log: Path,
    ledger_dir: Path,
    state_path: Path,
) -> List[Dict[str, Any]]:
    """Aggregate audit events, cron-log hits, ledger items, and
    cross-function breaches into a single list, oldest first.
    """
    since = now - timedelta(days=DEFAULT_WINDOW_DAYS)

    signals: List[Dict[str, Any]] = []
    signals.extend(_scan_audit_dir(audit_dir, function=function, since=since, until=now))
    signals.extend(_scan_cron_log(cron_log, function=function, since=since))
    signals.extend(_scan_ledger(ledger_dir, function=function, since=since))
    signals.extend(_scan_other_breaches(state_path, function=function, since=since))

    # Best-effort sort: signals carrying a `ts` go earliest-first; the
    # rest preserve insertion order at the end.
    def _key(s: Dict[str, Any]) -> Tuple[int, str]:
        ts = s.get("ts")
        if isinstance(ts, str):
            return (0, ts)
        return (1, "")

    signals.sort(key=_key)
    return signals


# ── public entry points ──────────────────────────────────────────────


def gather_diagnostic(
    breach: Breach,
    function_yaml: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    sources_base_dir: Optional[Path] = None,
    audit_dir: Optional[Path] = None,
    cron_log: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
    state_path: Optional[Path] = None,
) -> DiagnosticBundle:
    """Gather a 30-day diagnostic bundle for `breach`.

    All path arguments are dependency-injectable for tests. Defaults
    point at `~/.delimit/...`. The bundle is returned in memory only —
    callers are responsible for any persistence (the watcher persists
    nothing beyond the existing state file; `cli.diagnose` prints to
    stdout).
    """
    anchor = _breach_now(breach, now=now)
    since = anchor - timedelta(days=DEFAULT_WINDOW_DAYS)

    sources = _resolve_sources(
        function_yaml, sources_base_dir=sources_base_dir
    )

    audit_dir = audit_dir or USER_AUDIT_DIR
    cron_log = cron_log or USER_CRON_LOG
    ledger_dir = ledger_dir or USER_LEDGER_DIR
    state_path = state_path or USER_STATE_PATH

    # ─── activity ─────────────────────────────────────────────────────
    activity: Dict[str, Any] = {"window_days": DEFAULT_WINDOW_DAYS}
    evidence_paths: List[str] = []

    for source_name, source_path in sources.items():
        evidence_paths.append(str(source_path))
        records = _records_within(source_path, since=since, until=anchor)
        activity[source_name] = _activity_for_source(source_name, records)

        # Source-specific extras.
        if source_name == "drafts":
            extras = _social_drafts_extras(records)
            if extras:
                activity[source_name].update(extras)

        if source_name == "notifications":
            rate = _notify_success_rate(records)
            if rate is not None:
                activity[source_name]["notify_success_rate"] = rate

    # ─── trend ────────────────────────────────────────────────────────
    trend = _gather_trend(sources, now=anchor)

    # ─── baseline ────────────────────────────────────────────────────
    baseline = _gather_baseline(sources, now=anchor, breach=breach)

    # ─── related signals ─────────────────────────────────────────────
    related = _gather_related_signals(
        function=breach.function,
        now=anchor,
        audit_dir=audit_dir,
        cron_log=cron_log,
        ledger_dir=ledger_dir,
        state_path=state_path,
    )

    # Add the search roots to evidence_paths (founder may want to grep).
    for p in (audit_dir, cron_log, ledger_dir, state_path):
        if p.exists():
            evidence_paths.append(str(p))

    summary = _build_summary(breach, activity, trend, baseline)

    return DiagnosticBundle(
        breach=breach,
        function=breach.function,
        timestamp=anchor.isoformat(),
        summary=summary,
        activity_30d=activity,
        trend=trend,
        baseline=baseline,
        related_signals=related,
        evidence_paths=evidence_paths,
    )


def _build_summary(
    breach: Breach,
    activity: Dict[str, Any],
    trend: Dict[str, Any],
    baseline: Dict[str, Any],
) -> str:
    """One-line human-readable summary of the bundle."""
    drafts_30d = (activity.get("drafts") or {}).get("total")
    if drafts_30d is None:
        drafts_30d = 0

    # If literally every source is empty in window, say so plainly.
    any_data = False
    for key, val in activity.items():
        if key == "window_days":
            continue
        if isinstance(val, dict) and val.get("total"):
            any_data = True
            break
    if not any_data:
        return (
            f"{breach.function}/{breach.kpi_name} breach: no data in 30d window. "
            f"Function may be dead or sources are misconfigured."
        )

    rate = trend.get("drafts_per_day_last_7d")
    delta = trend.get("throughput_delta_pct")

    pieces = [
        f"{breach.function}/{breach.kpi_name} breach",
        f"actual={breach.actual} vs threshold={breach.threshold}",
        f"30d total drafts={drafts_30d}",
    ]
    if rate is not None:
        pieces.append(f"7d rate={rate}/day")
    if delta is not None:
        pieces.append(f"delta_vs_prior_7d={delta}%")
    return "; ".join(pieces)


# ── rendering ────────────────────────────────────────────────────────


def render_text(bundle: DiagnosticBundle) -> str:
    """Render a bundle as a plain-text email body.

    Format chosen for legibility in mutt/Gmail/etc; no HTML, no
    markdown rendering required. Sections appear in the same order as
    the dataclass fields so the founder reads "what happened → what's
    going on → trend → baseline → other signals".
    """
    lines: List[str] = []
    breach = bundle.breach

    lines.append("Self-repair diagnostic bundle (mode=diagnose).")
    lines.append("")
    lines.append(f"Summary:    {bundle.summary}")
    lines.append(f"Function:   {bundle.function}")
    lines.append(f"KPI:        {breach.kpi_name}")
    lines.append(f"Severity:   {breach.severity}")
    lines.append(f"Window:     {breach.window}")
    lines.append(f"Actual:     {breach.actual}")
    lines.append(f"Threshold:  {breach.threshold}")
    lines.append(f"Generated:  {bundle.timestamp}")
    if breach.rationale:
        lines.append("")
        lines.append("Rationale:")
        lines.append(breach.rationale)

    # ─ activity ──────────────────────────────────────────────────────
    lines.append("")
    lines.append("=== Activity (last 30 days) ===")
    for source_name, summary in bundle.activity_30d.items():
        if source_name == "window_days":
            continue
        if not isinstance(summary, dict):
            continue
        lines.append(f"")
        lines.append(f"-- {source_name} --")
        total = summary.get("total", 0)
        lines.append(f"  total: {total}")
        by_day = summary.get("by_day") or {}
        if by_day:
            # Show last 7 days inline; full series in JSON view.
            recent_days = list(by_day.items())[-7:]
            lines.append(
                "  last 7 days: "
                + ", ".join(f"{d}={n}" for d, n in recent_days)
            )
        for k, v in summary.items():
            if k in ("total", "by_day"):
                continue
            if isinstance(v, list) and v:
                preview = ", ".join(
                    f"{item.get('value', item)}={item.get('count', '?')}"
                    if isinstance(item, dict) else str(item)
                    for item in v[:5]
                )
                lines.append(f"  {k}: {preview}")
            elif isinstance(v, dict):
                preview = ", ".join(f"{kk}={vv}" for kk, vv in list(v.items())[:5])
                lines.append(f"  {k}: {preview}")
            else:
                lines.append(f"  {k}: {v}")

    # ─ trend ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("=== Trend (7d-now vs 7d-prior vs prior 30d) ===")
    if bundle.trend:
        for k, v in bundle.trend.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (no trend data — sources missing or empty)")

    # ─ baseline ──────────────────────────────────────────────────────
    lines.append("")
    lines.append("=== Baseline (all-time + last-known-good) ===")
    if bundle.baseline:
        for k, v in bundle.baseline.items():
            if isinstance(v, dict):
                inner = ", ".join(f"{kk}={vv}" for kk, vv in v.items())
                lines.append(f"  {k}: {{{inner}}}")
            else:
                lines.append(f"  {k}: {v}")
    else:
        lines.append("  (no baseline data)")

    # ─ related signals ───────────────────────────────────────────────
    lines.append("")
    lines.append("=== Related signals (last 30d) ===")
    if bundle.related_signals:
        for s in bundle.related_signals[:25]:
            ts = s.get("ts") or "?"
            kind = s.get("kind") or s.get("event") or "signal"
            summary = s.get("summary") or s.get("title") or s.get("line") or ""
            extra = ""
            if "function" in s and s["function"] != bundle.function:
                extra = f" (other function: {s['function']})"
            lines.append(f"  [{ts}] {kind}{extra} :: {summary}")
        if len(bundle.related_signals) > 25:
            lines.append(f"  ... and {len(bundle.related_signals) - 25} more")
    else:
        lines.append("  (none found)")

    # ─ evidence paths ───────────────────────────────────────────────
    lines.append("")
    lines.append("=== Evidence files ===")
    for p in bundle.evidence_paths:
        lines.append(f"  {p}")

    lines.append("")
    lines.append(
        "Mode: diagnose (evidence only — no deliberation, no fix application). "
        "Reply to this email to direct the next step."
    )
    return "\n".join(lines)


def render_json(bundle: DiagnosticBundle) -> Dict[str, Any]:
    """Return a JSON-serializable dict suitable for the eventual
    deliberate-mode prompt. The `breach` field is converted to a dict.
    """
    out: Dict[str, Any] = {
        "function": bundle.function,
        "timestamp": bundle.timestamp,
        "summary": bundle.summary,
        "breach": asdict(bundle.breach),
        "activity_30d": bundle.activity_30d,
        "trend": bundle.trend,
        "baseline": bundle.baseline,
        "related_signals": bundle.related_signals,
        "evidence_paths": bundle.evidence_paths,
    }
    return out
