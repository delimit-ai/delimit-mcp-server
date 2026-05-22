"""
Delimit self-repair loop (alert-mode MVP).

This package implements the lowest mode of the self-repair ladder: a KPI
watcher that emits founder notifications when a corp function breaches a
declared KPI floor or ceiling. Higher modes (diagnose / deliberate /
assist / full) are intentionally out of scope for this package.

See `/home/delimit/delimit-private/strategy/PROPOSED_SELF_REPAIR_LOOP.md`
for the full multi-mode architecture.

Internal-only. Not exposed as a public MCP tool. Imports are side-effect
free; the only file system writes happen through the explicit CLI
sub-commands (`delimit self-repair init`, `... set ...`, `... pause`).
"""

from .mode import (  # noqa: F401
    Mode,
    get_mode,
    load_config,
    set_mode,
)
from .kpi import (  # noqa: F401
    Breach,
    KpiResult,
    evaluate_function,
    evaluate_kpi,
    extract_breaches,
    load_function_kpis,
)
from .diagnose import (  # noqa: F401
    DiagnosticBundle,
    gather_diagnostic,
    render_json,
    render_text,
)
from .deliberate import (  # noqa: F401
    DEFAULT_DELIBERATION_TIMEOUT_SECONDS,
    DEFAULT_FIX_TIER,
    FIX_TIERS,
    DeliberationVerdict,
    render_verdict_email,
    run_deliberation,
    verdict_to_dict,
)
from .history import (  # noqa: F401
    DEFAULT_HISTORY_PATH,
    append_history,
    count_in_window,
    deliberations_this_week,
    iter_history,
    update_decision,
)

__all__ = [
    "Mode",
    "get_mode",
    "load_config",
    "set_mode",
    "Breach",
    "KpiResult",
    "evaluate_function",
    "evaluate_kpi",
    "extract_breaches",
    "load_function_kpis",
    "DiagnosticBundle",
    "gather_diagnostic",
    "render_json",
    "render_text",
    "DeliberationVerdict",
    "FIX_TIERS",
    "DEFAULT_FIX_TIER",
    "DEFAULT_DELIBERATION_TIMEOUT_SECONDS",
    "run_deliberation",
    "render_verdict_email",
    "verdict_to_dict",
    "append_history",
    "iter_history",
    "count_in_window",
    "deliberations_this_week",
    "update_decision",
    "DEFAULT_HISTORY_PATH",
]
