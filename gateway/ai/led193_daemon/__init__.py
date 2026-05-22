"""LED-193 autonomous daemon (MVP).

Cron-spawn, stateless, append-only audit. Picks ledger items tagged
``auto_execute=class_a:<profile>`` and executes a deterministic profile
(``format_fix``, ``lockfile_refresh``, ``docs_typo``) on a feature branch.
NEVER merges. Opens a PR for human review only after local pre-push gates
pass (security_audit + test_smoke + lint when applicable).

Panel decision (UNANIMOUS, 2026-05-07):
    `/home/delimit/delimit-private/deliberations/2026-05-07-led-193-autonomous-daemon-shape.md`

Design siblings the cron pattern of LED-1264 ``scan_bridge``:
    - cron-spawn (no long-running process)
    - lockfile concurrency=1
    - append-only audit log
    - kill switch via env var
    - circuit breakers (consecutive failures, daily caps)

Public entry points:

- :func:`picker.pick_next_item`      — ledger-item selection
- :func:`executor.execute_item`      — profile dispatch
- :func:`gate.run_pre_push_gate`     — local pre-push validation
- :func:`audit.log_execution`        — append-only execution log
- :func:`pause.is_paused` / :func:`pause.pause` / :func:`pause.clear`
- :func:`cost.check_caps` / :func:`cost.record_run`

The cron entry is :mod:`scripts.led193_cron`. Founder applies the
crontab line manually after review (NOT auto-installed).
"""

from ai.led193_daemon.audit import log_execution
from ai.led193_daemon.cost import check_caps, record_run
from ai.led193_daemon.executor import execute_item
from ai.led193_daemon.gate import run_pre_push_gate
from ai.led193_daemon.pause import clear as clear_pause
from ai.led193_daemon.pause import is_paused, pause as pause_daemon
from ai.led193_daemon.picker import pick_next_item

# Re-export the submodules so callers can do
# ``from ai.led193_daemon import audit, cost, executor, gate, pause, picker``
# without the function-named exports above shadowing the ``pause`` module.
from ai.led193_daemon import audit, cost, executor, gate, pause, picker  # noqa: E402,F401

__all__ = [
    "audit",
    "check_caps",
    "clear_pause",
    "cost",
    "execute_item",
    "executor",
    "gate",
    "is_paused",
    "log_execution",
    "pause",
    "pause_daemon",
    "pick_next_item",
    "picker",
    "record_run",
    "run_pre_push_gate",
]
