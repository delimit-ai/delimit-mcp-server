"""
Gate telemetry viewer for LED-1084 week 2 → week 3 transition.

Reads `[social-grounding-gate]` P3 ledger entries and summarizes:
  - score distribution (min, median, mean, p95, max)
  - flagged-vs-clean ratio per model
  - feature-detector status counts (clean / flagged-no-whitelist /
    flagged-unknown-specifics / error)
  - recent failing examples for qualitative review

Purpose: the Week 3 decision to flip `enforce_grounding=True` must be
data-driven. This tool reads the ledger the gate writes and produces a
snapshot the founder can review to decide the threshold + timing.

Usage:
    python -m ai.content_grounding.telemetry summary
    python -m ai.content_grounding.telemetry summary --days 14
    python -m ai.content_grounding.telemetry samples --n 5

JSON output for piping:
    python -m ai.content_grounding.telemetry summary --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.content_grounding.telemetry")

LEDGER_DIR = Path(os.environ.get("DELIMIT_LEDGER_DIR", str(Path.home() / ".delimit" / "ledger")))

# Parse the structured fields the gate bakes into its description text.
_SCORE_RE   = re.compile(r"^Score:\s*([0-9.]+)", re.MULTILINE)
_THRESH_RE  = re.compile(r"threshold:\s*([0-9.]+)", re.IGNORECASE)
_FEATURE_RE = re.compile(r"^Feature status:\s*(\w+)", re.MULTILINE)
_MODEL_RE   = re.compile(r"^Model:\s*(\S+)", re.MULTILINE)
_PLATFORM_RE = re.compile(r"^Platform:\s*(\w+)", re.MULTILINE)
_VENTURE_RE = re.compile(r"^Venture:\s*(\w+)", re.MULTILINE)


def _iter_gate_entries(since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Read every [social-grounding-gate] P3 ledger entry."""
    entries: List[Dict[str, Any]] = []
    if not LEDGER_DIR.is_dir():
        return entries
    for p in sorted(LEDGER_DIR.glob("*.jsonl")):
        try:
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = item.get("title", "")
                if "[social-grounding-gate]" not in title:
                    continue
                ts = item.get("created_at") or item.get("timestamp") or ""
                if since and ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt < since:
                            continue
                    except ValueError:
                        pass
                entries.append(item)
        except Exception as e:
            logger.debug("skipping %s: %s", p, e)
    return entries


def _parse_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the structured fields the gate logged in the description."""
    desc = item.get("description", "")
    score_m   = _SCORE_RE.search(desc)
    thresh_m  = _THRESH_RE.search(desc)
    feat_m    = _FEATURE_RE.search(desc)
    model_m   = _MODEL_RE.search(desc)
    platform_m = _PLATFORM_RE.search(desc)
    venture_m = _VENTURE_RE.search(desc)
    return {
        "id":        item.get("id", ""),
        "timestamp": item.get("created_at") or item.get("timestamp", ""),
        "score":     float(score_m.group(1)) if score_m else None,
        "threshold": float(thresh_m.group(1)) if thresh_m else None,
        "feat_status": feat_m.group(1) if feat_m else None,
        "model":     model_m.group(1) if model_m else None,
        "platform":  platform_m.group(1) if platform_m else None,
        "venture":   venture_m.group(1) if venture_m else None,
    }


def summarize(days: int = 14) -> Dict[str, Any]:
    """Aggregate stats for the last `days` of gate entries."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _iter_gate_entries(since=since)
    parsed = [_parse_entry(e) for e in raw]

    # Score stats (ignore entries with no score)
    scores = [p["score"] for p in parsed if p["score"] is not None]

    by_model: Dict[str, List[float]] = {}
    by_feat: Dict[str, int] = {}
    by_platform: Dict[str, int] = {}
    for p in parsed:
        m = p.get("model") or "unknown"
        by_model.setdefault(m, []).append(p["score"] if p["score"] is not None else 0.0)
        f = p.get("feat_status") or "unknown"
        by_feat[f] = by_feat.get(f, 0) + 1
        plat = p.get("platform") or "unknown"
        by_platform[plat] = by_platform.get(plat, 0) + 1

    def _stats(vals: List[float]) -> Dict[str, Any]:
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "min": round(min(vals), 3),
            "median": round(statistics.median(vals), 3),
            "mean": round(statistics.mean(vals), 3),
            "p95": round(sorted(vals)[int(len(vals) * 0.95)], 3) if len(vals) > 1 else vals[0],
            "max": round(max(vals), 3),
        }

    # Threshold inferred from first entry that has one (default 0.85).
    threshold = next((p["threshold"] for p in parsed if p["threshold"] is not None), 0.85)
    pass_count = sum(1 for s in scores if s >= threshold)
    fail_count = len(scores) - pass_count

    return {
        "window_days": days,
        "since": since.isoformat(),
        "total_entries": len(parsed),
        "threshold_inferred": threshold,
        "would_pass_count": pass_count,
        "would_fail_count": fail_count,
        "pass_rate": round(pass_count / max(1, len(scores)), 3),
        "score_stats": _stats(scores),
        "by_model": {m: _stats(v) for m, v in by_model.items()},
        "by_feat_status": by_feat,
        "by_platform": by_platform,
    }


def recent_samples(n: int = 5, days: int = 14) -> List[Dict[str, Any]]:
    """Return the N most-recent gate entries in parsed form."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _iter_gate_entries(since=since)
    # Latest last, so grab from the end
    return [_parse_entry(e) for e in raw[-n:]]


def _print_summary_text(s: Dict[str, Any]) -> None:
    print(f"Gate telemetry — last {s['window_days']} day(s)")
    print(f"  total entries: {s['total_entries']}")
    print(f"  threshold:     {s['threshold_inferred']}")
    print(f"  pass rate:     {s['pass_rate']:.1%}  ({s['would_pass_count']} pass / {s['would_fail_count']} fail)")
    ss = s.get("score_stats") or {}
    if ss.get("n"):
        print(f"  score stats:   n={ss['n']}  min={ss['min']}  median={ss['median']}  mean={ss['mean']}  p95={ss['p95']}  max={ss['max']}")
    if s.get("by_feat_status"):
        print("  feat_status:")
        for k, v in sorted(s["by_feat_status"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:12}  {v}")
    if s.get("by_platform"):
        print("  by platform:")
        for k, v in sorted(s["by_platform"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:10}  {v}")
    if s.get("by_model"):
        print("  by model:")
        for m, st in s["by_model"].items():
            if st.get("n"):
                print(f"    {m:16}  n={st['n']}  median={st['median']}  mean={st['mean']}")


def _main() -> int:
    parser = argparse.ArgumentParser(prog="ai.content_grounding.telemetry")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s_sum = sub.add_parser("summary", help="aggregate gate telemetry stats")
    s_sum.add_argument("--days", type=int, default=14)
    s_sum.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")

    s_samp = sub.add_parser("samples", help="recent gate entries, parsed")
    s_samp.add_argument("--days", type=int, default=14)
    s_samp.add_argument("--n", type=int, default=5)
    s_samp.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.cmd == "summary":
        s = summarize(days=args.days)
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            _print_summary_text(s)
        return 0

    if args.cmd == "samples":
        samples = recent_samples(n=args.n, days=args.days)
        if args.json:
            print(json.dumps(samples, indent=2))
        else:
            for s in samples:
                print(f"- {s['timestamp']}  model={s['model']}  score={s['score']}  feat={s['feat_status']}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())
