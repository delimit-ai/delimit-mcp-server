"""
Self-repair CLI.

Standalone argparse-based subcommands. Wired into the gateway CLI by
calling `python -m ai.self_repair.cli ...` (or directly via the `main`
entry point below).

Public-facing surface is intentionally narrow — this is internal-only
for the alert-mode MVP. No MCP tool registration.

Subcommands:
  status                 — print mode for each function in config
  set <fn> <mode>        — change a function's mode
  check [<fn>]           — evaluate KPIs once, print results
  pause                  — set global pause flag = true
  resume                 — set global pause flag = false
  init                   — copy bundled defaults into ~/.delimit/
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from .kpi import (
    KpiResult,
    evaluate_function,
    extract_breaches,
    load_function_kpis,
)
from .mode import (
    Mode,
    USER_CONFIG_PATH,
    load_config,
    set_global_pause,
    set_mode,
)
from .diagnose import gather_diagnostic, render_text as render_diagnostic_text
from .deliberate import (
    _build_context,
    _build_question,
    render_verdict_email,
    run_deliberation,
)
from .history import append_history, iter_history
from .watcher import WatcherDaemon, run_one_pass
from .apply import apply_by_history_id
from .verify import run_due_verifies

USER_DELIMIT_DIR = Path.home() / ".delimit"
USER_KPI_DIR = USER_DELIMIT_DIR / "function_kpi"
BUNDLED_DEFAULT_CONFIG = Path(__file__).parent / "default_self_repair.yaml"
BUNDLED_KPI_DIR = Path(__file__).parent / "default_kpis"


# ── handlers ─────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    from .mode import get_mode  # local import keeps the CLI importable in stripped envs

    cfg = load_config(args.config)
    fns = cfg.get("functions") or {}
    if not fns:
        print("(no functions declared in self_repair config)")
        return 0
    print(f"{'function':<40} {'mode':<12} pause")
    print("-" * 60)
    global_pause = bool(cfg.get("pause", False))
    if global_pause:
        print("(global pause is ON — every function effectively OFF)")
    for fn in sorted(fns.keys()):
        entry = fns[fn] or {}
        # Resolve through get_mode so we always print a canonical
        # Mode value (handles YAML 1.1 boolean quirks + env overrides).
        mode = get_mode(fn, cfg).value
        paused = bool(entry.get("pause", False))
        print(f"{fn:<40} {mode:<12} {'yes' if paused else 'no'}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    try:
        mode = Mode(args.mode.lower())
    except ValueError:
        print(
            f"error: unknown mode '{args.mode}'. valid: "
            f"{[m.value for m in Mode]}",
            file=sys.stderr,
        )
        return 2
    set_mode(args.function, mode, config_path=args.config)
    print(f"set {args.function} → {mode.value}")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    set_global_pause(True, config_path=args.config)
    print("self-repair: paused")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    set_global_pause(False, config_path=args.config)
    print("self-repair: resumed")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.function:
        fns = [args.function]
    else:
        fns = sorted((cfg.get("functions") or {}).keys())
    if not fns:
        print("(no functions to check)")
        return 0
    if args.dry_run:
        # Pure evaluation, no notify, no state.
        all_breaches = []
        for fn in fns:
            try:
                results = evaluate_function(fn)
            except FileNotFoundError as exc:
                print(f"{fn}: skipped — {exc}", file=sys.stderr)
                continue
            _print_results(fn, results)
            all_breaches.extend(extract_breaches(results, function=fn))
        if all_breaches and args.json:
            print(json.dumps([_breach_summary(b) for b in all_breaches], indent=2))
        return 0
    # Wire one-shot pass with notify enabled (real run, used in cron).
    fired = run_one_pass(functions=fns)
    print(f"alerts fired this pass: {len(fired)}")
    for b in fired:
        print(f"  - {b.function} :: {b.kpi_name} ({b.severity})")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Gather a diagnostic bundle for a function's first (or named) breach.

    Useful for tuning bundle contents without waiting for a real cron-
    triggered breach. Reads sources from ~/.delimit/ by default; pass
    --sources-base-dir to point at a fixture directory.
    """
    fn = args.function
    try:
        function_yaml = load_function_kpis(fn)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: cannot load KPI yaml for {fn}: {exc}", file=sys.stderr)
        return 2

    try:
        results = evaluate_function(
            fn, sources_base_dir=args.sources_base_dir
        )
    except FileNotFoundError as exc:
        print(f"error: KPI declaration missing for {fn}: {exc}", file=sys.stderr)
        return 2

    breaches = extract_breaches(results, function=fn)
    if not breaches:
        print(f"(no breaches for {fn} — nothing to diagnose)")
        return 0

    target = None
    if args.breach_name:
        for b in breaches:
            if b.kpi_name == args.breach_name:
                target = b
                break
        if target is None:
            print(
                f"error: --breach-name '{args.breach_name}' not in current "
                f"breaches: {[b.kpi_name for b in breaches]}",
                file=sys.stderr,
            )
            return 2
    else:
        target = breaches[0]

    bundle = gather_diagnostic(
        target,
        function_yaml,
        sources_base_dir=args.sources_base_dir,
    )
    if args.json:
        from .diagnose import render_json

        print(json.dumps(render_json(bundle), indent=2, default=str))
    else:
        print(render_diagnostic_text(bundle))
    return 0


def cmd_deliberate(args: argparse.Namespace) -> int:
    """Run evaluate_function → diagnose → deliberate end-to-end for one
    function. Useful for manually triggering a deliberation outside the
    cron-driven watcher.

    `--dry-run` prints the constructed question + evidence to stdout and
    does NOT call the deliberation engine. This is the prompt-tuning
    path — no transcripts are written, no history is appended.

    Without `--dry-run`, the verdict is recorded in self_repair_history
    and the (subject, body) of the email that would be sent is printed
    to stdout. The CLI does NOT actually send the email — that happens
    only inside the daemon path.
    """
    fn = args.function
    try:
        function_yaml = load_function_kpis(fn)
    except (FileNotFoundError, RuntimeError) as exc:
        print(
            f"error: cannot load KPI yaml for {fn}: {exc}", file=sys.stderr
        )
        return 2

    try:
        results = evaluate_function(
            fn, sources_base_dir=args.sources_base_dir
        )
    except FileNotFoundError as exc:
        print(
            f"error: KPI declaration missing for {fn}: {exc}",
            file=sys.stderr,
        )
        return 2

    breaches = extract_breaches(results, function=fn)
    if not breaches:
        print(f"(no breaches for {fn} — nothing to deliberate)")
        return 0

    target = None
    if args.breach_name:
        for b in breaches:
            if b.kpi_name == args.breach_name:
                target = b
                break
        if target is None:
            print(
                f"error: --breach-name '{args.breach_name}' not in current "
                f"breaches: {[b.kpi_name for b in breaches]}",
                file=sys.stderr,
            )
            return 2
    else:
        target = breaches[0]

    bundle = gather_diagnostic(
        target,
        function_yaml,
        sources_base_dir=args.sources_base_dir,
    )

    if args.dry_run:
        question = _build_question(target)
        context = _build_context(bundle)
        print("=== Question ===")
        print(question)
        print()
        print("=== Context ===")
        print(context)
        print()
        print(
            "(dry-run — deliberation engine NOT called; no transcript "
            "written, no history appended)"
        )
        return 0

    verdict = run_deliberation(target, bundle, function_yaml)
    try:
        append_history(verdict)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"warning: append_history failed: {exc}", file=sys.stderr
        )

    subject, body = render_verdict_email(verdict)
    print(f"Subject: {subject}")
    print()
    print(body)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Manually trigger the apply gate for a history record.

    Useful when the inbox executor missed an approval reply (e.g. the
    founder forwarded instead of replying, or replied from an account
    the daemon didn't recognize).
    """
    outcome = apply_by_history_id(args.history_id)
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, default=str))
    else:
        print(f"history_id:      {outcome.history_id}")
        print(f"fix_tier:        {outcome.fix_tier}")
        print(f"applied:         {outcome.applied}")
        print(f"error:           {outcome.error}")
        print(f"artifact_paths:  {outcome.artifact_paths}")
        print(f"rollback_token:  {outcome.rollback_token}")
        print(f"timestamp:       {outcome.timestamp}")
    return 0 if outcome.applied else 2


def cmd_verify(args: argparse.Namespace) -> int:
    """Run any due verify tasks once and print their outcomes."""
    outcomes = run_due_verifies()
    if not outcomes:
        print("(no verify tasks due)")
        return 0
    if args.json:
        print(
            json.dumps(
                [o.to_dict() for o in outcomes], indent=2, default=str
            )
        )
        return 0
    for o in outcomes:
        print(f"{o.fn_name} :: {o.kpi_name} — {o.status}")
        print(f"  history_id:    {o.history_id}")
        print(f"  actual_now:    {o.actual_now}")
        print(f"  threshold:     {o.threshold}")
        print(f"  prior_actual:  {o.prior_actual}")
        if o.escalated:
            print(f"  ESCALATED ({o.failed_fix_count} failed fixes)")
        print(f"  message:       {o.message}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Print the last N history records as a table."""
    rows = list(iter_history())
    if args.function:
        rows = [r for r in rows if r.get("function") == args.function]
    last_n = args.last or 20
    rows = rows[-last_n:]
    if not rows:
        print("(no history records)")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    # Compact table.
    print(
        f"{'ts':<32} {'function':<32} {'kpi':<24} {'tier':<16} "
        f"{'decision':<24}"
    )
    print("-" * 132)
    for r in rows:
        ts = str(r.get("ts") or "")[:31]
        fn = str(r.get("function") or "")[:31]
        kpi = str(r.get("breach_kpi") or "")[:23]
        tier = str(r.get("fix_tier") or "")[:15]
        decision = str(r.get("founder_decision") or "(pending)")[:23]
        print(f"{ts:<32} {fn:<32} {kpi:<24} {tier:<16} {decision:<24}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Control the self-repair watcher daemon (start/stop/status).

    Wraps `ai.self_repair_daemon` so an operator can flip the daemon on
    from the same CLI surface that runs `check`, `diagnose`, etc. The
    daemon itself runs in a background thread; this CLI simply calls
    start/stop/status and prints the resulting state dict.
    """
    from ai.self_repair_daemon import (
        start_daemon as _start,
        stop_daemon as _stop,
        get_daemon_status as _status,
    )

    if args.daemon_action == "start":
        kwargs: dict = {}
        if args.interval is not None:
            kwargs["poll_interval_seconds"] = args.interval
        if args.config is not None:
            kwargs["config_path"] = args.config
        result = _start(**kwargs)
    elif args.daemon_action == "stop":
        result = _stop()
    else:
        result = _status()

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") not in {"error"} else 2


def cmd_init(args: argparse.Namespace) -> int:
    """Copy bundled defaults into ~/.delimit/ if missing.

    Idempotent — never clobbers an existing user file. This is the
    ONLY entry point in the package that creates files in
    `~/.delimit/`. All other callers stay read-only on first import.
    """
    USER_DELIMIT_DIR.mkdir(parents=True, exist_ok=True)
    USER_KPI_DIR.mkdir(parents=True, exist_ok=True)

    actions: List[str] = []

    if not USER_CONFIG_PATH.exists():
        shutil.copyfile(BUNDLED_DEFAULT_CONFIG, USER_CONFIG_PATH)
        actions.append(f"created {USER_CONFIG_PATH}")
    else:
        actions.append(f"kept existing {USER_CONFIG_PATH}")

    for src in BUNDLED_KPI_DIR.glob("*.yaml"):
        dst = USER_KPI_DIR / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            actions.append(f"created {dst}")
        else:
            actions.append(f"kept existing {dst}")

    for line in actions:
        print(line)
    return 0


# ── helpers ──────────────────────────────────────────────────────────


def _print_results(fn: str, results: List[KpiResult]) -> None:
    print(f"\n=== {fn} ===")
    if not results:
        print("(no KPIs evaluated)")
        return
    width = max(len(r.name) for r in results)
    for r in results:
        if r.status == "ok":
            extra = (
                f"  actual={r.actual:.4g}  threshold={r.threshold}"
                if r.actual is not None and r.threshold is not None
                else ""
            )
        elif r.status == "breach":
            extra = (
                f"  ! actual={r.actual:.4g}  threshold={r.threshold} "
                f"({r.detail.get('kind', '?')}) [{r.severity}]"
            )
        elif r.status == "deferred":
            extra = "  (deferred — no measurable threshold)"
        else:
            extra = f"  error: {r.detail.get('error', 'unknown')}"
        print(f"  {r.name.ljust(width)}  {r.status.upper():<9}{extra}")


def _breach_summary(b: object) -> dict:
    from dataclasses import asdict

    return asdict(b)  # type: ignore[arg-type]


# ── argparse wiring ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delimit self-repair",
        description="Delimit self-repair alert-mode CLI (internal)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to self_repair.yaml (default: ~/.delimit/self_repair.yaml)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="show mode per function")
    p_status.set_defaults(handler=cmd_status)

    p_set = sub.add_parser("set", help="change a function's mode")
    p_set.add_argument("function", help="function name (e.g. marketing.social_outreach)")
    p_set.add_argument("mode", help="off|alert|diagnose|deliberate|assist|full")
    p_set.set_defaults(handler=cmd_set)

    p_check = sub.add_parser(
        "check", help="evaluate KPIs once (useful for tuning floors)"
    )
    p_check.add_argument(
        "function", nargs="?", default=None, help="single function to evaluate"
    )
    p_check.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate but do not send notifications or update state",
    )
    p_check.add_argument(
        "--json", action="store_true", help="emit breaches as JSON"
    )
    p_check.set_defaults(handler=cmd_check)

    p_pause = sub.add_parser("pause", help="set global pause flag")
    p_pause.set_defaults(handler=cmd_pause)

    p_resume = sub.add_parser("resume", help="clear global pause flag")
    p_resume.set_defaults(handler=cmd_resume)

    p_diag = sub.add_parser(
        "diagnose",
        help="gather and print a diagnostic bundle for a function's breach",
    )
    p_diag.add_argument(
        "function",
        help="function name (e.g. marketing.social_outreach)",
    )
    p_diag.add_argument(
        "--breach-name",
        default=None,
        help="specific KPI breach to diagnose (default: first breach found)",
    )
    p_diag.add_argument(
        "--sources-base-dir",
        type=Path,
        default=None,
        help="rebase ~/.delimit/ source paths under this directory (testing)",
    )
    p_diag.add_argument(
        "--json",
        action="store_true",
        help="emit the bundle as JSON instead of text",
    )
    p_diag.set_defaults(handler=cmd_diagnose)

    p_delib = sub.add_parser(
        "deliberate",
        help=(
            "run evaluate → diagnose → deliberate for a function and "
            "print the verdict email"
        ),
    )
    p_delib.add_argument(
        "function",
        help="function name (e.g. marketing.social_outreach)",
    )
    p_delib.add_argument(
        "--breach-name",
        default=None,
        help="specific KPI breach to deliberate (default: first breach found)",
    )
    p_delib.add_argument(
        "--sources-base-dir",
        type=Path,
        default=None,
        help="rebase ~/.delimit/ source paths under this directory (testing)",
    )
    p_delib.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the constructed question + context only; do NOT call "
            "the deliberation engine"
        ),
    )
    p_delib.set_defaults(handler=cmd_deliberate)

    p_apply = sub.add_parser(
        "apply",
        help="manually run the apply gate against a history record",
    )
    p_apply.add_argument(
        "history_id",
        help="action_id of the history record to apply (sr-...)",
    )
    p_apply.add_argument(
        "--json",
        action="store_true",
        help="emit the FixOutcome as JSON",
    )
    p_apply.set_defaults(handler=cmd_apply)

    p_verify = sub.add_parser(
        "verify", help="run any due verify tasks once"
    )
    p_verify.add_argument(
        "--json",
        action="store_true",
        help="emit verify outcomes as JSON",
    )
    p_verify.set_defaults(handler=cmd_verify)

    p_hist = sub.add_parser(
        "history", help="print recent self-repair history records"
    )
    p_hist.add_argument(
        "--function",
        default=None,
        help="filter by function name",
    )
    p_hist.add_argument(
        "--last",
        type=int,
        default=20,
        help="number of records to show (default 20)",
    )
    p_hist.add_argument(
        "--json",
        action="store_true",
        help="emit records as JSON",
    )
    p_hist.set_defaults(handler=cmd_history)

    p_init = sub.add_parser(
        "init", help="copy bundled defaults into ~/.delimit/ (idempotent)"
    )
    p_init.set_defaults(handler=cmd_init)

    p_daemon = sub.add_parser(
        "daemon",
        help="control the continuous self-repair watcher daemon",
    )
    p_daemon.add_argument(
        "daemon_action",
        choices=["start", "stop", "status"],
        help="start/stop/status of the background daemon",
    )
    p_daemon.add_argument(
        "--interval",
        type=int,
        default=None,
        help=(
            "pass interval in seconds (default: $DELIMIT_SELF_REPAIR_POLL"
            "_INTERVAL or 3600)"
        ),
    )
    p_daemon.set_defaults(handler=cmd_daemon)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
