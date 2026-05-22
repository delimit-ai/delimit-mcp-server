"""
Function-KPI evaluator.

Reads a function's KPI declaration (a YAML file in `default_kpis/` or in
`~/.delimit/function_kpi/`), evaluates each KPI against the declared
data sources, and returns structured results.

The measure DSL supported here is intentionally narrow — only the
patterns used by the bundled `social_outreach.yaml`:

  count(SOURCE)
  count(SOURCE where FIELD=VALUE)
  count(SOURCE where FIELD in (LIST_REF))
  count(distinct FIELD) where ts >= now - window
  count(...) / count(...)

Time windows always apply to a `ts` field on each JSONL record.
Records without a `ts` are excluded from windowed measures (with a
warning) — never silently counted as "in window".

Sources are JSONL files. Sources whose path is `null` in the yaml are
treated as deferred — KPIs that reference them return
`KpiResult(status='deferred')` and are NOT breaches.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger("delimit.ai.self_repair.kpi")

# Bundled KPI directory — used when ~/.delimit/function_kpi/ doesn't have it.
_BUNDLED_KPI_DIR = Path(__file__).parent / "default_kpis"

# Override directory on the user machine.
USER_KPI_DIR = Path.home() / ".delimit" / "function_kpi"


# ── data types ───────────────────────────────────────────────────────


@dataclass
class KpiResult:
    """Outcome of evaluating one KPI against current data."""

    name: str
    status: str  # 'ok' | 'breach' | 'deferred' | 'error'
    actual: Optional[float] = None
    threshold: Optional[float] = None
    severity: str = "warning"
    rationale: str = ""
    # Free-form extras: window, kind ('floor'/'ceiling'), source counts, etc.
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Breach:
    """A KPI breach, suitable for routing to delimit_notify."""

    function: str
    kpi_name: str
    severity: str
    actual: Optional[float]
    threshold: Optional[float]
    window: str
    rationale: str
    source_data_summary: Dict[str, Any]


# ── window parsing ───────────────────────────────────────────────────


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)


def parse_window(window: str) -> timedelta:
    """Parse a duration string like '24h', '7d', '30d', '6h', '1w'."""
    m = _WINDOW_RE.match(window or "")
    if not m:
        raise ValueError(f"invalid window: {window!r}")
    qty = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "s":
        return timedelta(seconds=qty)
    if unit == "m":
        return timedelta(minutes=qty)
    if unit == "h":
        return timedelta(hours=qty)
    if unit == "d":
        return timedelta(days=qty)
    if unit == "w":
        return timedelta(weeks=qty)
    raise ValueError(f"invalid window unit: {unit!r}")


# ── jsonl IO ─────────────────────────────────────────────────────────


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield records from a JSONL file. Skips bad lines with a debug log."""
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "self_repair: skipping bad jsonl line %s:%s — %s",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        logger.warning("self_repair: cannot read %s: %s", path, exc)
        return


def _record_ts(record: Dict[str, Any]) -> Optional[datetime]:
    """Parse the 'ts' field on a record. Returns None if missing or unparseable."""
    raw = record.get("ts")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            # Tolerate trailing 'Z'
            s = raw.rstrip("Z")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


# ── measure DSL ──────────────────────────────────────────────────────


# count(SOURCE) — bare
_COUNT_BARE_RE = re.compile(r"^\s*count\(\s*([a-zA-Z_][\w]*)\s*\)\s*$")
# count(SOURCE where FIELD = 'value')   or  count(SOURCE where FIELD=VALUE)
_COUNT_WHERE_EQ_RE = re.compile(
    r"^\s*count\(\s*([a-zA-Z_][\w]*)\s+where\s+([a-zA-Z_][\w]*)\s*=\s*"
    r"['\"]?([^'\")]+?)['\"]?\s*\)\s*$"
)
# count(SOURCE where FIELD in (REF))
_COUNT_WHERE_IN_RE = re.compile(
    r"^\s*count\(\s*([a-zA-Z_][\w]*)\s+where\s+([a-zA-Z_][\w]*)\s+in\s*"
    r"\(\s*([a-zA-Z_][\w]*)\s*\)\s*\)\s*$"
)
# count(distinct FIELD)
_COUNT_DISTINCT_RE = re.compile(
    r"^\s*count\(\s*distinct\s+([a-zA-Z_][\w]*)\s*\)\s*$"
)
# Captures the leading "count(...)" plus optional trailing window clause:
#     count(...)  where ts >= now - window
# We strip the trailing window clause before parsing the count expression.
_TRAILING_WINDOW_RE = re.compile(
    r"\s+where\s+ts\s*>=\s*now\s*-\s*window\s*$", re.IGNORECASE
)
_RATIO_RE = re.compile(r"^\s*(.+?)\s*/\s*(.+?)\s*$")


def _strip_trailing_window(expr: str) -> str:
    """Strip a trailing ` where ts >= now - window` clause from `expr`."""
    return _TRAILING_WINDOW_RE.sub("", expr).strip()


def _records_in_window(
    source_path: Path,
    window: timedelta,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return records whose `ts` falls within (now - window, now]."""
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - window
    out: List[Dict[str, Any]] = []
    for r in _iter_jsonl(source_path):
        ts = _record_ts(r)
        if ts is None:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def _all_records(source_path: Path) -> List[Dict[str, Any]]:
    return list(_iter_jsonl(source_path))


def _count_distinct_recent_targets(
    source_path: Path, field_name: str, lookback: timedelta, *, now: Optional[datetime] = None
) -> set:
    """Return the set of unique values for `field_name` within `lookback`.

    Used to resolve `IN (last_30d_targets)` style references in the
    duplicate-target measure.
    """
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - lookback
    out: set = set()
    for r in _iter_jsonl(source_path):
        ts = _record_ts(r)
        if ts is None or ts < cutoff:
            continue
        v = r.get(field_name)
        if v is not None:
            out.add(v)
    return out


def _resolve_count_expr(
    expr: str,
    sources: Dict[str, Path],
    window: timedelta,
    *,
    now: Optional[datetime] = None,
    in_refs: Optional[Dict[str, set]] = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Evaluate a single count(...) expression. Returns (value, summary).

    Returns `(None, {...})` if the expression references a deferred source.
    """
    expr = _strip_trailing_window(expr).strip()
    summary: Dict[str, Any] = {"expr": expr}

    # count(distinct FIELD) — expects window applied to the implicit
    # source. We treat it as "count distinct values of FIELD across all
    # configured sources combined" only when paired with a single source
    # — which matches the social_outreach.yaml usage of
    # `count(distinct platform)` over `drafts`.
    m = _COUNT_DISTINCT_RE.match(expr)
    if m:
        field_name = m.group(1)
        # Default to drafts source if exactly one source path is set;
        # otherwise we can't disambiguate and surface an error.
        active = [
            (name, p)
            for name, p in sources.items()
            if isinstance(p, Path)
        ]
        if len(active) != 1:
            return None, {
                "expr": expr,
                "error": (
                    "count(distinct ...) requires exactly one active "
                    "source; got "
                    f"{[n for n, _ in active]}"
                ),
            }
        src_name, src_path = active[0]
        records = _records_in_window(src_path, window, now=now)
        seen = {r.get(field_name) for r in records if r.get(field_name) is not None}
        summary.update(
            {
                "source": src_name,
                "records_in_window": len(records),
                "distinct_count": len(seen),
            }
        )
        return float(len(seen)), summary

    # count(SOURCE where FIELD in (REF))
    m = _COUNT_WHERE_IN_RE.match(expr)
    if m:
        src_name, field_name, ref_name = m.group(1), m.group(2), m.group(3)
        src_path = sources.get(src_name)
        if src_path is None:
            return None, {"expr": expr, "deferred_source": src_name}
        ref_set = (in_refs or {}).get(ref_name)
        if ref_set is None:
            return None, {"expr": expr, "error": f"unknown ref: {ref_name}"}
        records = _records_in_window(src_path, window, now=now)
        hits = [r for r in records if r.get(field_name) in ref_set]
        summary.update(
            {
                "source": src_name,
                "records_in_window": len(records),
                "matched": len(hits),
                "ref": ref_name,
                "ref_size": len(ref_set),
            }
        )
        return float(len(hits)), summary

    # count(SOURCE where FIELD = VALUE)
    m = _COUNT_WHERE_EQ_RE.match(expr)
    if m:
        src_name, field_name, value_raw = m.group(1), m.group(2), m.group(3)
        src_path = sources.get(src_name)
        if src_path is None:
            return None, {"expr": expr, "deferred_source": src_name}
        # Coerce simple types: 'true'/'false' → bool, digits → int
        v_lower = value_raw.lower()
        if v_lower in ("true", "false"):
            value: Any = v_lower == "true"
        elif value_raw.lstrip("-").isdigit():
            value = int(value_raw)
        else:
            value = value_raw
        records = _records_in_window(src_path, window, now=now)
        hits = [r for r in records if r.get(field_name) == value]
        summary.update(
            {
                "source": src_name,
                "records_in_window": len(records),
                "matched": len(hits),
                "filter": f"{field_name}={value!r}",
            }
        )
        return float(len(hits)), summary

    # count(SOURCE)
    m = _COUNT_BARE_RE.match(expr)
    if m:
        src_name = m.group(1)
        src_path = sources.get(src_name)
        if src_path is None:
            return None, {"expr": expr, "deferred_source": src_name}
        records = _records_in_window(src_path, window, now=now)
        summary.update(
            {
                "source": src_name,
                "records_in_window": len(records),
            }
        )
        return float(len(records)), summary

    return None, {"expr": expr, "error": "unparseable count expression"}


# ── KPI evaluator ────────────────────────────────────────────────────


def _build_in_refs(
    kpi_def: Dict[str, Any],
    sources: Dict[str, Path],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, set]:
    """Pre-compute named sets used by `IN (...)` clauses.

    The bundled social_outreach.yaml uses one such reference,
    `last_30d_targets`. We special-case it: pull the `target_id` field
    from the drafts source over the last 30d.
    """
    refs: Dict[str, set] = {}
    drafts = sources.get("drafts")
    if drafts is not None:
        refs["last_30d_targets"] = _count_distinct_recent_targets(
            drafts, "target_id", timedelta(days=30), now=now
        )
    return refs


def evaluate_kpi(
    kpi_def: Dict[str, Any],
    sources: Dict[str, Path],
    *,
    now: Optional[datetime] = None,
) -> KpiResult:
    """Evaluate a single KPI definition.

    `sources` maps source-name → Path. A source with value None is
    treated as deferred, and any KPI referencing it returns
    `status='deferred'`.
    """
    name = str(kpi_def.get("name", "<unnamed>"))
    severity = str(kpi_def.get("severity", "warning"))
    rationale = str(kpi_def.get("rationale", "") or "").strip()
    measure = str(kpi_def.get("measure", ""))
    window_str = str(kpi_def.get("window", ""))
    floor = kpi_def.get("floor", None)
    ceiling = kpi_def.get("ceiling", None)

    # Deferred KPI shortcut: floor=null AND ceiling=null OR a referenced
    # source is null.
    if floor is None and ceiling is None:
        return KpiResult(
            name=name,
            status="deferred",
            severity=severity,
            rationale=rationale,
            detail={"reason": "no floor or ceiling declared"},
        )

    try:
        window = parse_window(window_str)
    except ValueError as exc:
        return KpiResult(
            name=name,
            status="error",
            severity=severity,
            rationale=rationale,
            detail={"error": str(exc)},
        )

    in_refs = _build_in_refs(kpi_def, sources, now=now)

    # Ratio: A / B ?
    ratio_match = _RATIO_RE.match(measure)
    is_ratio = bool(
        ratio_match
        and "count(" in ratio_match.group(1)
        and "count(" in ratio_match.group(2)
    )

    if is_ratio:
        num_expr = ratio_match.group(1).strip()
        den_expr = ratio_match.group(2).strip()
        num, num_summary = _resolve_count_expr(
            num_expr, sources, window, now=now, in_refs=in_refs
        )
        den, den_summary = _resolve_count_expr(
            den_expr, sources, window, now=now, in_refs=in_refs
        )
        if num is None or den is None:
            return KpiResult(
                name=name,
                status="deferred",
                severity=severity,
                rationale=rationale,
                detail={"numerator": num_summary, "denominator": den_summary},
            )
        if den == 0:
            return KpiResult(
                name=name,
                status="deferred",
                severity=severity,
                rationale=rationale,
                detail={
                    "reason": "denominator is zero",
                    "numerator": num_summary,
                    "denominator": den_summary,
                },
            )
        actual = num / den
        detail: Dict[str, Any] = {
            "numerator": num_summary,
            "denominator": den_summary,
            "window": window_str,
        }
    else:
        actual_raw, summary = _resolve_count_expr(
            measure, sources, window, now=now, in_refs=in_refs
        )
        if actual_raw is None:
            return KpiResult(
                name=name,
                status="deferred",
                severity=severity,
                rationale=rationale,
                detail=summary,
            )
        actual = actual_raw
        detail = dict(summary)
        detail["window"] = window_str

    # Compare actual to floor / ceiling.
    if floor is not None and actual < float(floor):
        return KpiResult(
            name=name,
            status="breach",
            actual=actual,
            threshold=float(floor),
            severity=severity,
            rationale=rationale,
            detail={**detail, "kind": "floor"},
        )
    if ceiling is not None and actual > float(ceiling):
        return KpiResult(
            name=name,
            status="breach",
            actual=actual,
            threshold=float(ceiling),
            severity=severity,
            rationale=rationale,
            detail={**detail, "kind": "ceiling"},
        )
    return KpiResult(
        name=name,
        status="ok",
        actual=actual,
        threshold=float(floor) if floor is not None else (
            float(ceiling) if ceiling is not None else None
        ),
        severity=severity,
        rationale=rationale,
        detail={
            **detail,
            "kind": "floor" if floor is not None else "ceiling",
        },
    )


# ── function loader ──────────────────────────────────────────────────


def _kpi_yaml_path(function_name: str, base_dir: Optional[Path] = None) -> Path:
    """Resolve which yaml to read for `function_name`.

    Resolution order:
      1. Explicit `base_dir/<function_short>.yaml` if `base_dir` is set.
      2. `~/.delimit/function_kpi/<function_short>.yaml`
      3. Bundled `default_kpis/<function_short>.yaml`

    `function_short` strips the corp-area prefix
    (`marketing.social_outreach` → `social_outreach`).
    """
    short = function_name.split(".")[-1] if "." in function_name else function_name
    candidates: List[Path] = []
    if base_dir is not None:
        candidates.append(Path(base_dir) / f"{short}.yaml")
    candidates.append(USER_KPI_DIR / f"{short}.yaml")
    candidates.append(_BUNDLED_KPI_DIR / f"{short}.yaml")
    for c in candidates:
        if c.exists():
            return c
    # Return the bundled path so the caller surfaces a clear
    # missing-file error instead of resolving to None.
    return _BUNDLED_KPI_DIR / f"{short}.yaml"


def load_function_kpis(
    function_name: str, base_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Load the KPI yaml for `function_name`.

    Returns the parsed dict (always contains keys `function`, `kpis`,
    `sources`). Raises `FileNotFoundError` if no KPI file can be
    located.
    """
    if _yaml is None:
        raise RuntimeError("PyYAML is required to load KPI declarations")
    path = _kpi_yaml_path(function_name, base_dir=base_dir)
    if not path.exists():
        raise FileNotFoundError(f"KPI declaration not found for {function_name}: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"KPI yaml at {path} did not parse to a mapping")
    data.setdefault("kpis", [])
    data.setdefault("sources", {})
    data.setdefault("function", function_name)
    return data


def _resolve_sources(
    sources_block: Dict[str, Any],
    *,
    base_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """Convert the yaml `sources:` block to a {name: Path} map.

    Sources whose value is None are NOT included (caller treats missing
    keys as deferred). The optional `base_dir` allows tests to remap
    `~` to a fixture directory; if set, source values that begin with
    `~/.delimit/` are rewritten to live under `base_dir`.
    """
    out: Dict[str, Path] = {}
    if not isinstance(sources_block, dict):
        return out

    def _resolve_path(raw: Any) -> Optional[Path]:
        if raw is None:
            return None
        s = str(raw)
        if base_dir is not None and s.startswith("~/.delimit/"):
            tail = s[len("~/.delimit/") :]
            return Path(base_dir) / tail
        return Path(s).expanduser()

    for key, val in sources_block.items():
        if key == "external":
            # Each entry in `external:` is its own named source.
            if isinstance(val, dict):
                for ext_key, ext_val in val.items():
                    p = _resolve_path(ext_val)
                    if p is not None:
                        out[ext_key] = p
            continue
        p = _resolve_path(val)
        if p is not None:
            out[key] = p
    return out


def evaluate_function(
    function_name: str,
    base_dir: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
    sources_base_dir: Optional[Path] = None,
) -> List[KpiResult]:
    """Evaluate every KPI declared for `function_name`."""
    cfg = load_function_kpis(function_name, base_dir=base_dir)
    sources = _resolve_sources(cfg.get("sources", {}), base_dir=sources_base_dir)
    results: List[KpiResult] = []
    for kpi_def in cfg.get("kpis") or []:
        if not isinstance(kpi_def, dict):
            continue
        results.append(evaluate_kpi(kpi_def, sources, now=now))
    return results


def extract_breaches(
    results: List[KpiResult], *, function: str = ""
) -> List[Breach]:
    """Filter `results` to breaches and convert them to `Breach` objects."""
    out: List[Breach] = []
    for r in results:
        if r.status != "breach":
            continue
        out.append(
            Breach(
                function=function,
                kpi_name=r.name,
                severity=r.severity,
                actual=r.actual,
                threshold=r.threshold,
                window=str(r.detail.get("window", "")),
                rationale=r.rationale,
                source_data_summary={
                    k: v
                    for k, v in r.detail.items()
                    if k != "rationale"
                },
            )
        )
    return out
