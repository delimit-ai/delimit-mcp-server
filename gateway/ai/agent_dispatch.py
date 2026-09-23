"""Multi-agent orchestration — dispatch, track, and govern engineering tasks.

Agents are tracked via a local task store. Each dispatched task gets a unique
ID with status tracking. The dispatcher doesn't spawn actual AI processes
(that's the host AI's job) — it provides the governance layer:
assignment, tracking, policy enforcement, and handoff protocol.

Storage: ~/.delimit/agents/tasks.json
Audit trail: ~/.delimit/agents/audit.jsonl
"""

import json
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows has no fcntl; store locking degrades to in-process.
    fcntl = None  # type: ignore[assignment]
import functools
import inspect
import tempfile
import threading
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# LED-3759: reuse the shared ephemeral-path guard (single source; do not fork).
try:
    from ai.registry_guards import is_ephemeral_path
except ImportError:  # pragma: no cover - flat import layout
    from registry_guards import is_ephemeral_path

AGENTS_DIR = Path.home() / ".delimit" / "agents"
TASKS_FILE = AGENTS_DIR / "tasks.json"
AUDIT_FILE = AGENTS_DIR / "audit.jsonl"
PAUSE_FILE = Path.home() / ".delimit" / "pause_dispatch"

# LED-3759: the DEFAULT (production) store location, captured at import. The
# ephemeral guard only ever redirects writes away from THIS default store — if
# a caller/test has explicitly relocated ``AGENTS_DIR`` (e.g. to an isolated
# tmp dir), that override is honored verbatim and the guard stays out of the
# way. This is what keeps every existing test that patches AGENTS_DIR working.
_DEFAULT_AGENTS_DIR = AGENTS_DIR

# LED-3759: when the dispatch context (cwd or the supplied venture path) is an
# ephemeral test/scratch path, task + audit writes redirect here instead of the
# real ~/.delimit/agents store, so tests never leak a live record into
# tasks.json. Kept under the delimit home so it is session-scoped (removed with
# the tmp HOME under the test harness) rather than an ever-growing /tmp dir.
# Real contexts never touch it.
EPHEMERAL_AGENTS_DIR = Path.home() / ".delimit" / "_ephemeral_scratch" / "agents"


def _effective_agents_dir(venture: str = "") -> Path:
    """Return the agents store dir, redirected to a throwaway dir in tests.

    Fires on the *path* (LED-3733/LED-3759 design), never on "am I in pytest":
    a real cwd/venture is never redirected, only /tmp + pytest/tempfile scratch.
    An explicit store override (``AGENTS_DIR`` reassigned away from the import
    default) is always honored — the guard only protects the default store.
    """
    if AGENTS_DIR != _DEFAULT_AGENTS_DIR:
        return AGENTS_DIR
    if is_ephemeral_path(os.getcwd()) or (venture and is_ephemeral_path(venture)):
        return EPHEMERAL_AGENTS_DIR
    return AGENTS_DIR

VALID_PRIORITIES = {"P0", "P1", "P2"}
VALID_ASSIGNEES = {"claude", "codex", "gemini", "copilot", "muse", "any", "auto"}
VALID_STATUSES = {"dispatched", "in_progress", "done", "handed_off", "failed",
                  "running", "completed", "cancelled", "uncertain",
                  "launch_refused", "launch_unsupported", "exhausted"}

# LED-5321 M5: assignee runtimes with a headless launch contract. LED-5314:
# codex/copilot/antigravity gain contracts
# (scripts/launch_contained_worker_{codex,copilot,antigravity}.sh) alongside
# muse (scripts/launch_contained_worker.sh). Runtimes without a contract
# record launch_unsupported when launch= is requested — no headless contract
# is invented for them. "any" routing is unchanged (it still resolves to
# claude/codex/gemini and records launch_unsupported when launch= is given,
# even for codex — backward compatibility for a customer-facing parameter);
# "auto" is the NEW policy-order rotating value.
_LAUNCH_RUNTIMES = frozenset({"muse", "copilot", "codex", "antigravity"})
VALID_LAUNCH_NETWORKS = frozenset({"proxy-only", "restricted", "enabled"})
_LAUNCH_DEFAULT_MAX_STEPS = 220
_LAUNCH_DEFAULT_NETWORK = "proxy-only"

# LED-5314: owner routing policy (2026-09-14, reaffirmed 2026-09-20) for
# assignee="auto": muse → copilot → antigravity → codex → claude. Claude has
# no headless launch contract, so rotation stops before it ("exhausted") and
# the lead decides. Metered API routes are never used by "auto".
_AUTO_POLICY_ORDER = ("muse", "copilot", "antigravity", "codex")

# LED-5314: CLI binary a runtime needs on PATH to be attempted.
_RUNTIME_CLI = {"muse": "muse", "copilot": "copilot",
                "antigravity": "agy", "codex": "codex"}

# LED-5321 M5: statuses owned by the tracked-launch lifecycle. complete_task
# gates on this set: launched tasks may only close from completed/uncertain
# (uncertain needs accept_uncertain=True); legacy statuses (dispatched /
# in_progress / handed_off / done / failed) close exactly as before.
# LED-5314: "exhausted" (auto rotation ran out of runtimes) is terminal like
# launch_refused — the lead re-dispatches instead of closing it.
_LAUNCHED_LIFECYCLE_STATUSES = frozenset({
    "running", "completed", "cancelled", "uncertain",
    "launch_refused", "launch_unsupported", "exhausted",
})

# LED-5321 M5: tracked-worker lease + run-output layout under the agents dir.
_WORKER_LEASE_SECONDS = 4 * 3600
_WORKER_RUNS_SUBDIR = "runs"

# LED-5321 M5: terminal event markers written by `muse exec --json`. poll_worker
# matches these against the type/event/name/kind field of each JSONL object
# (structured match only — no inference, no substring scan of prose).
TERMINAL_COMPLETED_EVENT = "run.terminal.completed"
TERMINAL_FAILED_EVENT = "run.terminal.failed"
LIFECYCLE_EVENT = "task.lifecycle"

# LED-5314: terminal markers for the newer runtimes (structured match only).
# codex exec --json streams {"type": ...} events (turn.completed carries
# usage, turn.failed carries error); agy --output-format stream-json streams
# {"event": ...} events with one terminal {"event": "result"} verdict;
# copilot --output-format json streams {"type": ...} events ending in a
# {"type": "result"} event (a {"type": "error"} or {"type":
# "session.error"} event means failure).
CODEX_TURN_COMPLETED = "turn.completed"
CODEX_TURN_FAILED = "turn.failed"
CODEX_ERROR = "error"
AGY_RESULT_EVENT = "result"
COPILOT_RESULT_EVENT = "result"
COPILOT_ERROR_EVENT = "error"
COPILOT_SESSION_ERROR_EVENT = "session.error"
_RUNTIME_TERMINAL_HINT = {
    "codex": "turn.completed/turn.failed",
    "antigravity": "result",
    "copilot": "result/session.error",
}
_AGY_SUCCESS_STATUSES = frozenset({
    "success", "successful", "succeeded", "ok", "completed", "complete", "done",
})

# LED-876: auto-pause when dead-letter queue depth (stuck 'dispatched' tasks)
# hits this threshold. Prevents runaway dispatch when no workers are pulling.
DLQ_AUTO_PAUSE_THRESHOLD = 20

# LED-3514: task types whose lifecycle parks them at status=='dispatched' as a
# long-lived TARGET POOL rather than a worker-consumption queue. These are NOT
# a dead-letter backlog — they are read in place (e.g. outreach_substantive is
# the target pool that body-gen + the contributions dashboard consume), so they
# must be excluded from the DLQ depth or they spuriously trip the circuit
# breaker and globally pause ALL dispatch. The breaker still protects genuine
# worker-consumed task types; pool growth is bounded by each pool's own caps
# (the outreach daemon enforces a per-day dispatch cap + repo cooldown).
_DLQ_EXEMPT_TASK_TYPES = frozenset({"outreach_substantive"})

# LED-878: router table — resolves assignee='any' to a specific model at
# dispatch time based on task_type. This eliminates the dead-letter 'any'
# bucket without requiring a worker process to exist yet. The mapping is
# deliberately conservative: if the task type is unknown, fall through to
# gemini (cheapest, highest throughput) rather than pile onto claude.
TASK_TYPE_ROUTER = {
    # Outreach and social work — Gemini Flash is fast and cheap
    "outreach": "gemini",
    # LED-2214b: substantive github outreach gets the same default
    # routing as generic outreach (cheap, fast drafter) but is named
    # distinctly so a regression that resurrects the generic dispatch
    # path does not silently land here.
    "outreach_substantive": "gemini",
    "social": "gemini",
    "content": "gemini",
    "sensor": "gemini",
    # Engineering — Claude / Codex for code, Claude for governance
    "fix": "claude",
    "feat": "claude",
    "refactor": "claude",
    "test": "codex",
    "research": "gemini",
    "strategy": "gemini",
    "deliberation": "claude",
}
ROUTER_DEFAULT_ASSIGNEE = "gemini"

# STR-2202: concrete (non-"any") assignees the rank resolver may pick.
# Copilot is an explicit execution harness, not an automatic rank target.
_CONCRETE_ASSIGNEES = frozenset({"claude", "codex", "gemini"})

# STR-2202: minimum recorded outcomes for a model before prompt_drift.rank is
# trusted to resolve assignee="any". Below this the data is too thin, so we
# FALL BACK to the static TASK_TYPE_ROUTER (today's behavior). This is the
# fail-safe threshold — rank never *blocks* a dispatch, it only refines it.
_RANK_MIN_SAMPLES = 5

# STR-2202: variables keys that carry an explicit checkout/worktree path a
# dispatch will write into. Only when one is present do we auto-claim a
# checkout lock (fold 3) — without an explicit target we cannot safely
# identify the checkout, so we skip the claim rather than over-block.
_CHECKOUT_HINT_KEYS = ("checkout", "worktree", "checkout_path", "repo_path")

# Task types / result markers used to derive prompt_drift.success at completion.
_FAILURE_MARKERS = ("failed", "error:", "blocked", "could not", "unable to", "aborted")


def _resolve_any_assignee(task_type: str) -> str:
    """Resolve assignee='any' to a concrete model.

    STR-2202: prompt_drift.rank is the PRIMARY resolver — it picks the
    best-performing model for this task_type from RECORDED outcomes
    (Optimal-Tooling by mechanism). When data is thin or anything errors,
    fall back to the static TASK_TYPE_ROUTER (fail-safe — never blocks
    dispatch). Returns a concrete assignee name.
    """
    tt = (task_type or "").lower().strip()
    ranked = _rank_best_assignee(tt)
    if ranked:
        return ranked
    return TASK_TYPE_ROUTER.get(tt, ROUTER_DEFAULT_ASSIGNEE)


def _rank_best_assignee(task_type: str) -> Optional[str]:
    """Best concrete model per prompt_drift rankings, or None if data is thin.

    Best-effort: any failure returns None so the caller falls back to the
    static router. A model must have >= _RANK_MIN_SAMPLES recorded outcomes
    for its ranking to count.
    """
    try:
        from ai.prompt_drift import get_model_rankings

        ranked = get_model_rankings(task_type=task_type)
        if not isinstance(ranked, dict) or ranked.get("status") != "ok":
            return None
        # rankings are pre-sorted best-first (success rate desc, duration asc).
        for row in ranked.get("rankings", []):
            model = (row.get("model") or "").lower().strip()
            if model in _CONCRETE_ASSIGNEES and int(row.get("total_tasks", 0)) >= _RANK_MIN_SAMPLES:
                return model
        return None
    except Exception:  # pragma: no cover — resolver must never break dispatch
        return None


def _checkout_target(
    variables: Optional[Dict[str, Any]],
    constraints: Optional[List[str]],
) -> Optional[str]:
    """Return the checkout/worktree path this dispatch should claim, or None.

    Only claims when an explicit checkout hint is present in ``variables`` and
    the task is not read-only. No hint → None → no claim (safe, no over-block).
    """
    cons = {(c or "").lower().strip() for c in (constraints or [])}
    if "read-only" in cons or "readonly" in cons:
        return None
    for key in _CHECKOUT_HINT_KEYS:
        val = (variables or {}).get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _iso_epoch(ts: str) -> Optional[float]:
    """Parse a '%Y-%m-%dT%H:%M:%SZ' UTC timestamp to epoch seconds, or None."""
    if not ts:
        return None
    try:
        import calendar
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def _record_completion_drift(task: Dict[str, Any]) -> None:
    """Fold prompt_drift.record into the completion path (STR-2202 fold 1).

    Best-effort + FAIL-SAFE: any error here must NEVER change the completion
    result. Derives {model, task_type, success, duration} from data already on
    the task record — no separate manual call.
    """
    try:
        from ai.prompt_drift import record_result

        model = (task.get("assignee") or "").strip()
        if model == "auto":
            model = (task.get("runtime") or "").strip()
        if not model or model == "any":
            return
        result = (task.get("result") or "").strip()
        title = (task.get("title") or "").strip()
        # Stable-ish prompt identity for hashing/grouping across models.
        prompt = title or (task.get("context") or "").strip() or task.get("id", "")
        if not prompt:
            return

        task_type = (task.get("task_type") or "").strip() or "engineering"
        # success = task closed with a result and no explicit failure marker.
        low = result.lower()
        success = bool(result) and not any(m in low for m in _FAILURE_MARKERS)

        start = _iso_epoch(task.get("created_at", ""))
        end = _iso_epoch(task.get("completed_at", "")) or _iso_epoch(task.get("updated_at", ""))
        duration_ms = 0
        if start is not None and end is not None and end >= start:
            duration_ms = int((end - start) * 1000)

        record_result(
            prompt=prompt,
            model=model,
            result_summary=result or "(no result summary)",
            success=success,
            task_type=task_type,
            duration_ms=duration_ms,
        )
    except Exception:  # pragma: no cover — instrumentation must never break completion
        pass


def _release_checkout_claim(task: Dict[str, Any]) -> None:
    """Release any checkout lock this task holds (STR-2202 fold 3, best-effort)."""
    try:
        from ai.collision_detect import release_checkout

        release_checkout(task_id=task.get("id", ""))
    except Exception:  # pragma: no cover — release must never break completion/handoff
        pass


def _repo_root() -> Path:
    """Return the delimit-gateway repo root (parent of ai/)."""
    return Path(__file__).resolve().parent.parent


# LED-5321 M5: contained-worker launcher (PR #532). Monkeypatchable in tests;
# DELIMIT_WORKER_LAUNCHER env also overrides at call time.
# LED-5314: one launcher script per runtime; each is monkeypatchable and has
# its own DELIMIT_WORKER_LAUNCHER_<RUNTIME> override (resolved dynamically so
# test monkeypatching keeps working).
LAUNCHER_SCRIPT = _repo_root() / "scripts" / "launch_contained_worker.sh"
LAUNCHER_SCRIPT_CODEX = _repo_root() / "scripts" / "launch_contained_worker_codex.sh"
LAUNCHER_SCRIPT_COPILOT = _repo_root() / "scripts" / "launch_contained_worker_copilot.sh"
LAUNCHER_SCRIPT_ANTIGRAVITY = _repo_root() / "scripts" / "launch_contained_worker_antigravity.sh"

_LAUNCHER_ENV_BY_RUNTIME = {
    "muse": "DELIMIT_WORKER_LAUNCHER",
    "codex": "DELIMIT_WORKER_LAUNCHER_CODEX",
    "copilot": "DELIMIT_WORKER_LAUNCHER_COPILOT",
    "antigravity": "DELIMIT_WORKER_LAUNCHER_ANTIGRAVITY",
}


def _resolve_launcher(runtime: str = "muse") -> Path:
    """Return the launcher script path for a runtime, honoring its env override."""
    env_key = _LAUNCHER_ENV_BY_RUNTIME.get(runtime or "muse", "DELIMIT_WORKER_LAUNCHER")
    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override)
    if runtime == "codex":
        return LAUNCHER_SCRIPT_CODEX
    if runtime == "copilot":
        return LAUNCHER_SCRIPT_COPILOT
    if runtime == "antigravity":
        return LAUNCHER_SCRIPT_ANTIGRAVITY
    return LAUNCHER_SCRIPT


def _lead_identity() -> str:
    """Return the dispatching lead's id for the worker lease owner field."""
    explicit = os.environ.get("DELIMIT_LEAD_ID", "").strip()
    if explicit:
        return explicit
    try:
        host = socket.gethostname()
    except Exception:
        host = "localhost"
    return f"lead@{host}:{os.getpid()}"


def _utcnow() -> str:
    """Current UTC timestamp in the task-store format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_launch_spec(launch: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a caller-provided launch spec; return (normalized, error).

    Shape errors (wrong type, unknown network, bad max_steps) are caller bugs
    and return an error BEFORE any task is created. Missing files / tools are
    environmental and surface later as a recorded launch_refused, not here.
    """
    if launch is None:
        return None, None
    if not isinstance(launch, dict):
        return None, "launch must be a dict like {'workspace': ..., 'prompt_file': ...}"
    if not launch:
        return None, None  # empty dict == no launch (matches _coerce_dict_arg(""))
    workspace = launch.get("workspace", "")
    prompt_file = launch.get("prompt_file", "")
    if not workspace or not str(workspace).strip():
        return None, "launch.workspace is required (git worktree path)"
    if not prompt_file or not str(prompt_file).strip():
        return None, "launch.prompt_file is required"
    max_steps = launch.get("max_steps", _LAUNCH_DEFAULT_MAX_STEPS)
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        try:
            max_steps = int(str(max_steps).strip())
        except (ValueError, TypeError):
            return None, "launch.max_steps must be a positive int"
    if max_steps <= 0:
        return None, "launch.max_steps must be a positive int"
    network = str(launch.get("network", _LAUNCH_DEFAULT_NETWORK)).strip().lower()
    if network not in VALID_LAUNCH_NETWORKS:
        return None, (
            f"launch.network must be one of: {', '.join(sorted(VALID_LAUNCH_NETWORKS))}"
        )
    return {
        "workspace": str(workspace).strip(),
        "prompt_file": str(prompt_file).strip(),
        "max_steps": max_steps,
        "network": network,
    }, None


def _launcher_has_yolo(content: str) -> bool:
    """True when the launcher would actually pass --yolo to something.

    Token-aware: full-line and trailing `#` comments are ignored (the real
    script documents the --yolo ban in its own header), while a real flag
    token anywhere in code trips the guard. Unparseable content fails
    closed on a raw substring match.
    """
    import shlex
    try:
        tokens = shlex.split(content, comments=True, posix=True)
    except ValueError:
        return "--yolo" in content
    return any(t == "--yolo" or t.startswith("--yolo=") for t in tokens)


# LED-5314: per-runtime flags that must never appear as real tokens in a
# launcher script (a flag that disables a sandbox or path verification).
# The guard is token-aware like _launcher_has_yolo, so each script may still
# document its own ban in comments — and, e.g., copilot's REQUIRED
# --allow-all-tools never trips the --allow-all ban (exact-token match, not
# a prefix match).
_LAUNCHER_FORBIDDEN_FLAGS = {
    "muse": frozenset({"--yolo"}),
    "codex": frozenset({"--yolo", "--dangerously-bypass-approvals-and-sandbox"}),
    "antigravity": frozenset({"--yolo", "--dangerously-skip-permissions"}),
    "copilot": frozenset({"--yolo", "--allow-all", "--allow-all-paths"}),
}


def _launcher_forbidden_flag(content: str, runtime: str) -> Optional[str]:
    """First forbidden flag token the launcher would actually pass, or None.

    Token-aware: full-line and trailing `#` comments are ignored, while a
    real flag token anywhere in code trips the guard. Unparseable content
    fails closed on a raw substring match.
    """
    import shlex
    forbidden = _LAUNCHER_FORBIDDEN_FLAGS.get(runtime or "muse", frozenset({"--yolo"}))
    try:
        tokens = shlex.split(content, comments=True, posix=True)
    except ValueError:
        for flag in sorted(forbidden):
            if flag in content:
                return flag
        return None
    for tok in tokens:
        for flag in sorted(forbidden):
            if tok == flag or tok.startswith(flag + "="):
                return flag
    return None


def _check_launcher_preconditions(
    launcher: Path, spec: Dict[str, Any], runtime: str = "muse",
) -> Optional[str]:
    """Return a refusal reason, or None if the launcher may run.

    Every failure here is environmental (missing tooling/files, unsafe
    launcher) and maps to a recorded launch_refused — never a crash. Muse
    keeps its bubblewrap gate; the newer runtimes rely on CLI-native
    containment (codex --sandbox, agy --sandbox, copilot path verification)
    and gate on their CLI being installed instead.
    """
    if not launcher.is_file():
        return f"launcher script missing: {launcher}"
    try:
        content = launcher.read_text()
    except OSError as e:
        return f"launcher script unreadable: {e}"
    if (runtime or "muse") == "muse":
        if _launcher_has_yolo(content):
            return "launcher contains forbidden --yolo flag; refusing to launch"
        if shutil.which("bwrap") is None:
            return "bubblewrap (bwrap) missing: sandbox cannot start; refusing to launch"
    else:
        bad = _launcher_forbidden_flag(content, runtime)
        if bad is not None:
            return f"launcher contains forbidden {bad} flag; refusing to launch"
        cli = _RUNTIME_CLI.get(runtime, "")
        if cli and shutil.which(cli) is None:
            return f"{cli} not installed; refusing to launch"
    ws = spec["workspace"]
    if not os.path.isdir(ws):
        return f"workspace is not a directory: {ws}"
    if not (os.path.isdir(os.path.join(ws, ".git")) or os.path.isfile(os.path.join(ws, ".git"))):
        return f"workspace is not a git worktree: {ws}"
    if not os.path.isfile(spec["prompt_file"]):
        return f"prompt file missing: {spec['prompt_file']}"
    return None


def _worktree_branch(workspace: str) -> str:
    """Best-effort current branch of a worktree; '' when unknowable."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip()
            return "" if branch == "HEAD" else branch
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _find_worker_pid(session_id: str) -> Optional[int]:
    """Best-effort pid of the live worker holding session_id; None if absent."""
    if not session_id:
        return None
    self_pid = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        if int(pid) == self_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
        except (OSError, ValueError):
            continue
        if session_id in cmdline:
            return int(pid)
    return None


def _read_first_line(path: Path) -> str:
    """Best-effort first line of a small launcher sidecar file."""
    try:
        return path.read_text().splitlines()[0].strip() if path.exists() else ""
    except (OSError, IndexError):
        return ""


def _launch_runtime_worker(
    *,
    runtime: str,
    task_id: str,
    spec: Dict[str, Any],
    agents_dir: Path,
    output_prefix: Optional[Path] = None,
) -> Dict[str, Any]:
    """Start a worker through its runtime's contained launcher.

    Same lifecycle for every runtime (task record, lease, output prefix, rc
    sidecar, poll, cancel, uncertain): no sandbox-disabling flag, ever.
    Returns the binding record (runtime/session_id/workspace/branch/
    started_at/output_prefix/lease/worker_pid) or {"error": reason} when the
    launch must be refused. The launcher subprocess is given argv only — never
    shell=True — and the script itself backgrounds the worker.
    """
    launcher = _resolve_launcher(runtime)
    refusal = _check_launcher_preconditions(launcher, spec, runtime)
    if refusal is not None:
        return {"error": refusal}
    runs_dir = agents_dir / _WORKER_RUNS_SUBDIR
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"cannot create worker runs dir: {e}"}
    out_prefix = output_prefix if output_prefix is not None else runs_dir / task_id
    cmd = [
        str(launcher),
        spec["workspace"],
        spec["prompt_file"],
        str(out_prefix),
        str(spec["max_steps"]),
        spec["network"],
        spec.get("session_id", ""),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "launcher timed out after 120s", "execution_uncertain": True}
    except OSError as e:
        return {"error": f"launcher exec failed: {e}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1][:300] if detail else f"exit {proc.returncode}"
        pre_spawn = (_read_first_line(Path(str(out_prefix) + ".launch-protocol")) == "1"
                     and not Path(str(out_prefix) + ".spawn-attempted").exists())
        return {"error": f"launcher refused (exit {proc.returncode}): {tail}",
                "execution_uncertain": not pre_spawn}
    session_id = _read_first_line(Path(str(out_prefix) + ".session-id"))
    if not session_id or (spec.get("session_id") and session_id != spec["session_id"]):
        return {"error": "launcher session binding missing or mismatched", "execution_uncertain": True}
    started_at = _read_first_line(Path(str(out_prefix) + ".started")) or _utcnow()
    now_epoch = time.time()
    return {
        "runtime": runtime,
        "session_id": session_id,
        "workspace": spec["workspace"],
        "branch": _worktree_branch(spec["workspace"]),
        "started_at": started_at,
        "output_prefix": str(out_prefix),
        "lease": {
            "owner": _lead_identity(),
            "acquired_at": _utcnow(),
            "expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(now_epoch + _WORKER_LEASE_SECONDS),
            ),
        },
        "worker_pid": _find_worker_pid(session_id),
    }


def _launch_muse_worker(
    *,
    task_id: str,
    spec: Dict[str, Any],
    agents_dir: Path,
    output_prefix: Optional[Path] = None,
) -> Dict[str, Any]:
    """Start a muse worker through the contained launcher (no --yolo, ever).

    Thin wrapper over _launch_runtime_worker; same behavior as before.
    """
    return _launch_runtime_worker(
        runtime="muse", task_id=task_id, spec=spec,
        agents_dir=agents_dir, output_prefix=output_prefix,
    )


# ── LED-5314: auto rotation state (reused, never a new store) ──────────

# Rotation reuses the EXISTING subscription-CLI quota-hold store from
# ai/social.py (LED-4912: ~/.delimit/state/social_cli_quota_holds.json —
# {mid: {reason, until, monthly, set_at}}) with the same TTL semantics, so a
# quota-exhausted provider is skipped by worker dispatch and social drafting
# alike. Quota/auth failures go through social's monthly-reset-aware setter;
# launch refusals share the file/schema/bounded window with their own reason.
# Only when ai.social is unimportable does dispatch fall back to one small
# JSON file under the agents dir (same schema).
_DISPATCH_HOLD_REASON_REFUSED = "launch_refused"
_DISPATCH_HOLDS_FALLBACK_FILE = "runtime_holds.json"

# A poll-caught terminal failure rotates to the next runtime only when it
# lands within this window after the worker started (a quota wall kills the
# run fast; a late failure is real work the lead should inspect) AND the
# output carries a quota/rate-limit signal.
_AUTO_QUICK_FAIL_SECONDS = 600
_AUTO_QUOTA_SIGNAL_RE = re.compile(
    r"429|402|quota|rate[\s_-]?limit|resource[\s_-]?exhausted|"
    r"usage[\s_-]?limit|limit[\s_-]?reached|out of credits?|"
    r"insufficient[\s_-](credit|quota|balance)",
    re.IGNORECASE,
)


def _dispatch_hold_path(agents_dir: Optional[Path] = None) -> Path:
    """Path of the shared quota-hold file (or the agents-dir fallback)."""
    try:
        from ai.social import _social_cli_hold_path
    except Exception:
        try:
            from social import _social_cli_hold_path
        except Exception:
            base = agents_dir or _effective_agents_dir()
            return base / _DISPATCH_HOLDS_FALLBACK_FILE
    try:
        return _social_cli_hold_path()
    except Exception:
        base = agents_dir or _effective_agents_dir()
        return base / _DISPATCH_HOLDS_FALLBACK_FILE


def _dispatch_holds_active() -> bool:
    """Mirror social's holds gate (inactive under tests without an override)."""
    try:
        from ai.social import _social_cli_holds_active
    except Exception:
        try:
            from social import _social_cli_holds_active
        except Exception:
            return True  # fallback file is already test-isolated (AGENTS_DIR)
    try:
        return bool(_social_cli_holds_active())
    except Exception:
        return False


def _dispatch_read_holds(agents_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Best-effort read of the shared holds file (same schema as social)."""
    try:
        state = json.loads(_dispatch_hold_path(agents_dir).read_text())
    except (OSError, ValueError):
        return {}
    return dict(state) if isinstance(state, dict) else {}


def _dispatch_runtime_hold(runtime: str) -> Optional[Dict[str, Any]]:
    """Active unavailability hold for a dispatch runtime, or None.

    Best-effort and fail-safe: any error (or an inactive gate) means "no
    hold" so dispatch is never blocked by bookkeeping.
    """
    if not _dispatch_holds_active():
        return None
    try:
        entry = _dispatch_read_holds().get(runtime)
        now = time.time()
        if (isinstance(entry, dict)
                and isinstance(entry.get("until"), (int, float))
                and entry["until"] > now):
            return entry
    except Exception:
        pass
    return None


def _dispatch_hold_window_seconds() -> int:
    """Bounded hold window — same knob and clamp as social's setter."""
    try:
        return max(60, min(6 * 3600, int(os.environ.get(
            "DELIMIT_SOCIAL_CLI_QUOTA_HOLD_SEC", "1800"))))
    except ValueError:
        return 1800


def _dispatch_write_hold(mid: str, reason: str, until: float,
                         *, monthly: bool = False,
                         agents_dir: Optional[Path] = None) -> float:
    """Write a hold entry to the shared file (best-effort, same schema).

    Returns the hold-until epoch, or 0.0 when the gate is inactive or the
    write failed. Never raises into dispatch.
    """
    if not _dispatch_holds_active():
        return 0.0
    now = time.time()
    try:
        from datetime import datetime, timezone
        set_at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    except Exception:
        set_at = _utcnow()
    try:
        path = _dispatch_hold_path(agents_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        holds = _dispatch_read_holds(agents_dir)
        holds[mid] = {"reason": reason, "until": until,
                      "monthly": bool(monthly), "set_at": set_at}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(holds))
        os.replace(tmp, path)
        return until
    except OSError:
        return 0.0


def _dispatch_mark_quota(runtime: str, evidence: str = "") -> float:
    """Hold a quota-failed runtime via social's setter (monthly-aware).

    Falls back to a local bounded-window write when ai.social is
    unimportable. Returns the hold-until epoch, or 0.0 when no hold applies.
    """
    try:
        from ai.social import _social_cli_set_hold
    except Exception:
        try:
            from social import _social_cli_set_hold
        except Exception:
            monthly = "monthly" in (evidence or "").lower() and "quota" in (evidence or "").lower()
            if monthly:
                import calendar
                t = time.gmtime()
                y, m = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
                until = float(calendar.timegm((y, m, 1, 0, 0, 0)))
            else:
                until = time.time() + _dispatch_hold_window_seconds()
            return _dispatch_write_hold(runtime, "quota", until, monthly=monthly)
    try:
        return _social_cli_set_hold(runtime, "quota", evidence or "")
    except Exception:
        return 0.0


def _dispatch_mark_refused(runtime: str) -> float:
    """Record nothing shared for a refused launch; return 0.0.

    A refusal is usually local (missing worktree/prompt/CLI/sandbox), not a
    provider capacity signal. Writing it to the shared LED-4912 holds file
    would pause live social drafting on that provider, so refusals stay
    task-local: the task's ``attempts`` history already records them and
    rotation never retries a runtime within one task. Only quota/rate-limit
    evidence (``_dispatch_mark_quota``) writes the shared hold.
    """
    return 0.0


def _auto_skip_reason(runtime: str) -> Optional[str]:
    """None if the runtime is eligible now, else a skip reason (no side effects)."""
    cli = _RUNTIME_CLI.get(runtime, "")
    if cli and shutil.which(cli) is None:
        return f"{cli} not installed"
    hold = _dispatch_runtime_hold(runtime)
    if hold:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(float(hold.get("until", 0))))
        except (TypeError, ValueError, OverflowError):
            when = "unknown time"
        return f"on {hold.get('reason', 'quota')} hold until {when}"
    return None


def _auto_attempt_entry(runtime: str, outcome: str, reason: str) -> Dict[str, Any]:
    """One attempt-history row: runtime, outcome, reason, timestamps."""
    return {"runtime": runtime, "outcome": outcome, "reason": (reason or "")[:300],
            "attempted_at": _utcnow()}


def _auto_exhausted_error(attempts: List[Dict[str, Any]]) -> str:
    """One-line rotation summary for the exhausted task record."""
    parts = [f"{a.get('runtime')}: {a.get('reason')}" for a in attempts
             if a.get("runtime") != "claude"]
    return "rotation exhausted: " + ("; ".join(parts) if parts else "no runtime attempted")


def _check_auto_spec_once(spec: Dict[str, Any]) -> Optional[str]:
    """Caller-bug precheck for auto: fail fast, don't burn one hold per runtime."""
    ws = spec["workspace"]
    if not os.path.isdir(ws):
        return f"workspace is not a directory: {ws}"
    if not (os.path.isdir(os.path.join(ws, ".git")) or os.path.isfile(os.path.join(ws, ".git"))):
        return f"workspace is not a git worktree: {ws}"
    if not os.path.isfile(spec["prompt_file"]):
        return f"prompt file missing: {spec['prompt_file']}"
    return None


def _dispatch_auto_launch(
    task: Dict[str, Any],
    tasks: Dict[str, Any],
    norm_launch: Dict[str, Any],
    agents_dir: Path,
) -> None:
    """Policy-order rotation for assignee="auto". Mutates task/tasks in place.

    Tries muse → copilot → antigravity → codex, skipping CLIs that are
    missing or on quota hold; a clean launch refusal holds the runtime and
    falls through to the next. At most one attempt per runtime. Success binds
    the worker (status running); total failure records "exhausted" (stopping
    before claude — no headless contract — so the lead can decide). An
    uncertain launch (a worker may have started) stops rotation as
    launch_refused instead of risking a duplicate worker. Persists before
    every spawn, mirroring the explicit-launch path.
    """
    from ai.agent_session_slots import reserve
    task_id = task["id"]
    attempts: List[Dict[str, Any]] = []
    base_prefix = agents_dir / _WORKER_RUNS_SUBDIR / task_id
    task["run_base_prefix"] = str(base_prefix)
    task["launch_seq"] = 0
    spec_error = _check_auto_spec_once(norm_launch)
    if spec_error is not None:
        task["status"] = "launch_refused"
        task["launch_error"] = spec_error
        task["attempts"] = attempts
        return
    from ai.provider_usage import choose_runtime
    selection = choose_runtime(task["priority"], task.get("task_type", ""))
    recorded_skips = set()
    def record_skips_through(runtime: str) -> bool:
        if runtime not in _AUTO_POLICY_ORDER:
            attempts.append(_auto_attempt_entry(runtime, "skipped", "runtime absent from auto policy order"))
            return False
        for candidate in _AUTO_POLICY_ORDER[:_AUTO_POLICY_ORDER.index(runtime) + 1]:
            if candidate in selection["skipped"] and candidate not in recorded_skips:
                attempts.append(_auto_attempt_entry(candidate, "skipped", selection["skipped"][candidate]))
                recorded_skips.add(candidate)
        return True
    for runtime in selection["ranked"]:
        if not record_skips_through(runtime):
            continue
        skip = _auto_skip_reason(runtime)
        if skip is not None:
            attempts.append(_auto_attempt_entry(runtime, "skipped", skip))
            continue
        seq = int(task.get("launch_seq", 0) or 0)
        prefix = base_prefix if seq == 0 else Path(str(base_prefix) + f".auto{seq}")
        task["execution_slot"] = reserve(prefix, str(uuid.uuid4()))
        tasks[task_id] = task
        _save_tasks(tasks, agents_dir)
        launch_spec = dict(norm_launch, session_id=task["execution_slot"]["session_id"])
        if runtime == "muse":
            binding = _launch_muse_worker(
                task_id=task_id, spec=launch_spec, agents_dir=agents_dir,
                output_prefix=prefix,
            )
        else:
            binding = _launch_runtime_worker(
                runtime=runtime, task_id=task_id, spec=launch_spec,
                agents_dir=agents_dir, output_prefix=prefix,
            )
        task["execution_slot"]["launch_returned"] = True
        task["launch_seq"] = seq + 1
        if "error" in binding:
            if binding.get("execution_uncertain"):
                attempts.append(_auto_attempt_entry(
                    runtime, "refused",
                    binding["error"] + " (execution uncertain; rotation stopped)"))
                task["attempts"] = attempts
                task["status"] = "launch_refused"
                task["launch_error"] = binding["error"]
                return
            _dispatch_mark_refused(runtime)
            task["execution_slot"]["state"] = "released"
            attempts.append(_auto_attempt_entry(runtime, "refused", binding["error"]))
            continue
        attempts.append(_auto_attempt_entry(
            runtime, "launched",
            f"worker started (session {binding['session_id']})"))
        task["status"] = "running"
        task.update(binding)
        task["resume_count"] = 0
        task["output_history"] = []
        task["attempts"] = attempts
        return
    record_skips_through(_AUTO_POLICY_ORDER[-1])
    attempts.append(_auto_attempt_entry(
        "claude", "skipped", "no headless launch contract; lead decides"))
    task["attempts"] = attempts
    task["status"] = "exhausted"
    task["launch_error"] = _auto_exhausted_error(attempts)


def _ensure_dir(base_dir: Optional[Path] = None):
    """Create the agents directory if it doesn't exist."""
    (base_dir or _effective_agents_dir()).mkdir(parents=True, exist_ok=True)


_store_thread_lock = threading.RLock()
_store_lock_local = threading.local()


class TaskStoreUnavailable(RuntimeError):
    pass


def _serialized_store(func):
    """Serialize full lifecycle transactions across MCP processes and threads."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        base = _effective_agents_dir(bound.arguments.get("venture", ""))
        with _store_thread_lock:
            held = getattr(_store_lock_local, "held", set())
            if base in held:
                return func(*args, **kwargs)
            _ensure_dir(base)
            with (base / "tasks.lock").open("a+") as lock:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                _store_lock_local.held = held | {base}
                try:
                    return func(*args, **kwargs)
                except TaskStoreUnavailable as exc:
                    return {"status": "store_unavailable", "error": str(exc)}
                finally:
                    _store_lock_local.held = held
                    if fcntl is not None:
                        fcntl.flock(lock, fcntl.LOCK_UN)
    return wrapped


def _store_read_safe(**on_unavailable):
    """Read-only store paths return the structured store_unavailable result
    (plus any caller-specific fields) instead of raising to the MCP client."""
    def deco(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except TaskStoreUnavailable as exc:
                return {"status": "store_unavailable", "error": str(exc), **on_unavailable}
        return wrapped
    return deco


def _load_tasks(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load the store; corruption must not turn into an empty worker pool."""
    tasks_file = (base_dir or _effective_agents_dir()) / "tasks.json"
    try:
        tasks = json.loads(tasks_file.read_text())
    except FileNotFoundError:
        runs = tasks_file.parent / _WORKER_RUNS_SUBDIR
        if runs.exists() and any(runs.iterdir()):
            raise TaskStoreUnavailable("Task store missing with worker artifacts; admission held")
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise TaskStoreUnavailable("Task store unreadable; admission held") from exc
    if not isinstance(tasks, dict) or any(not isinstance(t, dict) for t in tasks.values()):
        raise TaskStoreUnavailable("Task store malformed; admission held")
    return tasks


def _save_tasks(tasks: Dict[str, Any], base_dir: Optional[Path] = None):
    """Atomically persist a transaction before launching any worker."""
    base = base_dir or _effective_agents_dir()
    _ensure_dir(base)
    fd, name = tempfile.mkstemp(prefix=".tasks-", dir=base)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(tasks, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, base / "tasks.json")
        directory_fd = os.open(base, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _append_audit(entry: Dict[str, Any], base_dir: Optional[Path] = None):
    """Append an entry to the audit trail."""
    base = base_dir or _effective_agents_dir()
    _ensure_dir(base)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(base / "audit.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


@_serialized_store
def dispatch_task(
    title: str,
    description: str = "",
    assignee: str = "any",
    priority: str = "P1",
    tools_needed: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    context: str = "",
    task_type: str = "",
    venture: str = "",
    variables: Optional[Dict[str, Any]] = None,
    external_key: str = "",
    focus_bypass_reason: str = "",
    launch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a tracked agent task.

    LED-1829 (T3 focus gate): ``focus_bypass_reason`` is an optional one-line
    reason recorded when dispatching NON-Delimit engineering work while an
    advanceable Delimit P0/P1 remains open. This is ADVISORY only — it never
    blocks the dispatch; it attaches a ``focus_advisory`` block to the result
    and logs the bypass for the 30-day review. See ai/focus_gate.py.

    LED-5321 M5: ``launch`` is an optional dict
    ``{"workspace": <git worktree path>, "prompt_file": <path>,
    "max_steps": int, "network": "proxy-only|restricted|enabled"}``.
    ``launch.network`` is validated for every runtime but only ENFORCED
    by the muse launcher (``--sandbox-network``); codex/antigravity/
    copilot use their CLI's own sandbox network defaults. When
    given and the assignee runtime has a headless contract (``muse``,
    ``codex``, ``copilot``), the worker is started through its
    ``scripts/launch_contained_worker*.sh`` script (subprocess, never a
    sandbox-disabling flag) and the task records
    runtime/session_id/workspace/branch/started_at/output_prefix/lease with
    status ``running``. Missing preconditions (no sandbox tooling, bad
    workspace, unsafe launcher) record ``launch_refused``. Any other assignee
    records ``launch_unsupported`` — no headless contract is invented.
    LED-5314: assignee ``auto`` (requires ``launch``) rotates through
    muse → copilot → antigravity → codex in policy order, skipping runtimes
    whose CLI is missing or on quota hold; a refused or quick quota-failed
    runtime is held and the SAME task is re-launched on the next runtime
    (attempt history on the task record). Total failure records
    ``exhausted``. Without ``launch`` the dispatch is audit-only with status
    ``dispatched``, exactly as before.

    Returns:
        Dict with task_id, task details, and a structured prompt for the host AI.
    """
    if not title or not title.strip():
        return {"error": "title is required"}

    # LED-3759: resolve the effective store once, honoring an ephemeral venture
    # path in addition to an ephemeral cwd. Threaded into every task/audit write
    # below so a dispatch from a test context never touches the real store.
    agents_dir = _effective_agents_dir(venture)

    # LED-876: reject ghost "[VENTURE] Engage:  on x" titles with empty author
    # slot. The social_target fix drops these at the scanner, but keep this as
    # a belt-and-suspenders check since agent_dispatch has other callers too.
    stripped = title.strip()
    if "Engage:  on " in stripped or "Engage: on " in stripped:
        return {"error": f"rejected ghost engage task with empty author: {stripped!r}"}

    # LED-876: manual kill switch. Touch ~/.delimit/pause_dispatch to halt all
    # dispatches instantly without touching loop_config. Remove the file to
    # resume. Kept deliberately simple so it works from any shell.
    if PAUSE_FILE.exists():
        _append_audit({
            "action": "dispatch_rejected_paused",
            "title": stripped,
            "reason": str(PAUSE_FILE),
        }, agents_dir)
        return {"error": f"dispatch paused: {PAUSE_FILE} exists"}

    # LED-876: automatic circuit breaker. If the DLQ (count of 'dispatched'
    # tasks that never moved to in_progress/done/failed) exceeds the threshold,
    # auto-create the pause file and reject. This stops the cycle from growing
    # the queue unboundedly when workers aren't consuming.
    # LED-3514: exclude pool-lifecycle task types (e.g. outreach_substantive)
    # from the depth — they live at 'dispatched' by design and are not a stuck
    # backlog, so counting them spuriously pauses all dispatch.
    existing_tasks = _load_tasks(agents_dir)
    dlq_depth = sum(
        1 for t in existing_tasks.values()
        if t.get("status") == "dispatched"
        and t.get("task_type") not in _DLQ_EXEMPT_TASK_TYPES
    )
    if dlq_depth >= DLQ_AUTO_PAUSE_THRESHOLD:
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(
            f"auto-paused at {time.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"(dlq_depth={dlq_depth} >= {DLQ_AUTO_PAUSE_THRESHOLD})\n"
        )
        _append_audit({
            "action": "dispatch_auto_paused",
            "dlq_depth": dlq_depth,
            "threshold": DLQ_AUTO_PAUSE_THRESHOLD,
        }, agents_dir)
        return {
            "error": (
                f"auto-paused: DLQ depth {dlq_depth} >= {DLQ_AUTO_PAUSE_THRESHOLD}. "
                f"Clear stuck tasks then delete {PAUSE_FILE} to resume."
            )
        }

    assignee = assignee.lower().strip() if assignee else "any"
    if assignee not in VALID_ASSIGNEES:
        return {"error": f"assignee must be one of: {', '.join(sorted(VALID_ASSIGNEES))}"}

    # LED-878: resolve 'any' to a specific model via the router table so
    # tasks never land in a bucket no worker pulls from. The mapping uses
    # task_type as the primary key; if unknown, falls through to the
    # default (gemini — cheapest + highest throughput).
    # LED-5314: routed_from_any is sticky — an "any"-routed task keeps the
    # exact pre-rotation launch behavior (launch_unsupported) even when it
    # routes to a runtime that gained a headless contract, so customer
    # calls never newly spawn workers (backward compatibility).
    routed_from_any = (assignee == "any")
    if assignee == "any":
        # STR-2202: prompt_drift.rank is the primary resolver behind "any";
        # it falls back to the static TASK_TYPE_ROUTER when data is thin or on
        # any error (fail-safe — resolution never blocks the dispatch).
        routed = _resolve_any_assignee(task_type)
        if routed in _CONCRETE_ASSIGNEES:
            assignee = routed
        else:
            assignee = ROUTER_DEFAULT_ASSIGNEE

    is_auto = (assignee == "auto")

    priority = priority.upper().strip() if priority else "P1"
    if priority not in VALID_PRIORITIES:
        return {"error": f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}"}

    # LED-5321 M5: malformed launch specs are caller bugs — fail fast before
    # any write, mirroring the invalid-assignee path above.
    norm_launch, launch_spec_error = _validate_launch_spec(launch)
    if launch_spec_error is not None:
        return {"error": launch_spec_error}

    # LED-5314: "auto" is a launch mode, not a worker bucket — without a
    # launch spec there is nothing to rotate, so fail fast (no compat
    # constraint: "auto" is new).
    if is_auto and norm_launch is None:
        return {"error": "assignee 'auto' requires a launch spec (policy-order contained launch)"}

    # LED-1279: commit mentions may indicate duplicate work, but cannot prove
    # an LED is complete. Report matches without blocking or closing the LED.
    possible_duplicate: Optional[Dict[str, Any]] = None
    try:
        from ai.dispatch_gate import evaluate_dispatch, extract_led_id, lookup_led_created_at

        led_id_for_gate = extract_led_id(title, description, context)
        if led_id_for_gate:
            led_created_at = lookup_led_created_at(led_id_for_gate)
            possible_duplicate = evaluate_dispatch(
                title=title,
                description=description,
                context=context,
                led_created_at=led_created_at,
            )
            if possible_duplicate is not None:
                _append_audit({
                    "action": "dispatch_possible_duplicate",
                    "title": stripped,
                    "led_id": possible_duplicate.get("led_id"),
                    "matching_commits": [c.get("short_sha") for c in possible_duplicate.get("commits", [])],
                }, agents_dir)
    except Exception as e:  # pragma: no cover — gate must never crash dispatch
        # If the gate itself blows up, log it and proceed — losing a dispatch
        # to a gate bug is a worse failure mode than the duplicate it would
        # have caught.
        _append_audit({
            "action": "dispatch_gate_error",
            "error": str(e)[:200],
        }, agents_dir)

    # LED-1829 (T3 primary-venture focus gate): ADVISORY + LOGGED, never blocks.
    # When dispatching non-Delimit engineering work while an advanceable Delimit
    # P0/P1 remains open, emit an advisory and record a (standing or supplied)
    # bypass reason. Proceeding is always allowed — this is a nudge, not a gate.
    focus_advisory: Optional[Dict[str, Any]] = None
    try:
        from ai.focus_gate import evaluate_focus

        focus_advisory = evaluate_focus(
            venture=venture,
            task=title,
            task_type=task_type,
            description=description,
            context=context,
            bypass_reason=focus_bypass_reason,
        )
        if focus_advisory is not None:
            _append_audit({
                "action": "focus_gate_advisory",
                "title": stripped,
                "venture": venture,
                "warn": focus_advisory.get("warn"),
                "standing": focus_advisory.get("standing"),
            }, agents_dir)
    except Exception as e:  # pragma: no cover — advisory must never crash dispatch
        _append_audit({
            "action": "focus_gate_error",
            "error": str(e)[:200],
        }, agents_dir)

    tasks = _load_tasks(agents_dir)

    normalized_external_key = external_key.strip()
    if normalized_external_key:
        for existing in tasks.values():
            if existing.get("external_key") != normalized_external_key:
                continue
            if existing.get("status") in ("dispatched", "in_progress", "handed_off", "done"):
                prompt = _build_agent_prompt(existing)
                deduped_resp = {
                    "status": "deduped",
                    "task_id": existing["id"],
                    "task": existing,
                    "agent_prompt": prompt,
                    "message": f"Task {existing['id']} already exists for {normalized_external_key}",
                }
                if focus_advisory is not None:
                    deduped_resp["focus_advisory"] = focus_advisory
                if possible_duplicate is not None:
                    deduped_resp["possible_duplicate"] = possible_duplicate
                return deduped_resp

    if norm_launch is not None and (
            is_auto or (assignee in _LAUNCH_RUNTIMES and not routed_from_any)):
        from ai.agent_session_slots import admission
        held = admission(tasks)
        if held:
            return held

    task_id = f"AGT-{uuid.uuid4().hex[:8].upper()}"

    # STR-2202 (fold 3): auto-claim the target checkout at dispatch start when
    # the dispatch carries an explicit checkout/worktree hint and is not
    # read-only. A CONFLICT is a HARD STOP — we refuse the dispatch so two
    # tasks never write the same checkout state. A plumbing ERROR (not a
    # conflict) is fail-safe: log and proceed with the dispatch unclaimed.
    checkout_lock_key = ""
    checkout_target = _checkout_target(variables, constraints)
    if checkout_target:
        try:
            from ai.collision_detect import claim_checkout

            claim = claim_checkout(checkout_target, model=assignee, task_id=task_id)
            if isinstance(claim, dict) and claim.get("status") == "collision":
                _append_audit({
                    "action": "dispatch_blocked_checkout_collision",
                    "title": stripped,
                    "checkout": checkout_target,
                    "held_by_task": claim.get("held_by_task", ""),
                }, agents_dir)
                return {
                    "error": (
                        f"checkout collision: {checkout_target} is held by "
                        f"{claim.get('held_by_task') or claim.get('held_by')} "
                        f"since {claim.get('held_since', 'unknown')}. "
                        f"Use an isolated worktree or wait."
                    ),
                    "status": "blocked_collision",
                    "collision": claim,
                }
            if isinstance(claim, dict) and claim.get("status") == "claimed":
                checkout_lock_key = claim.get("lock_key", "")
        except Exception as e:  # pragma: no cover — claim plumbing is fail-safe
            _append_audit({
                "action": "dispatch_checkout_claim_error",
                "title": stripped,
                "error": str(e)[:200],
            }, agents_dir)

    task = {
        "id": task_id,
        "title": title.strip(),
        "description": description.strip(),
        "assignee": assignee,
        "priority": priority,
        "tools_needed": tools_needed or [],
        "constraints": constraints or [],
        "context": context.strip(),
        "task_type": task_type.strip(),
        "venture": venture.strip(),
        "variables": variables or {},
        "external_key": normalized_external_key,
        "status": "dispatched",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files_changed": [],
        "result": "",
        "handoffs": [],
        "checkout_lock": checkout_lock_key,
    }

    # LED-5321 M5: tracked launch. The binding (or refusal) is recorded on the
    # task BEFORE the single save below, so the store never shows a launched
    # task as audit-only 'dispatched'.
    if norm_launch is not None:
        task["launch"] = norm_launch
        if is_auto:
            _dispatch_auto_launch(task, tasks, norm_launch, agents_dir)
        elif assignee in _LAUNCH_RUNTIMES and not routed_from_any:
            from ai.agent_session_slots import reserve
            prefix = agents_dir / _WORKER_RUNS_SUBDIR / task_id
            task["execution_slot"] = reserve(prefix, str(uuid.uuid4()))
            tasks[task_id] = task
            _save_tasks(tasks, agents_dir)
            launch_spec = dict(norm_launch, session_id=task["execution_slot"]["session_id"])
            if assignee == "muse":
                binding = _launch_muse_worker(
                    task_id=task_id, spec=launch_spec, agents_dir=agents_dir,
                )
            else:
                binding = _launch_runtime_worker(
                    runtime=assignee, task_id=task_id, spec=launch_spec,
                    agents_dir=agents_dir,
                )
            task["execution_slot"]["launch_returned"] = True
            if "error" in binding:
                task["status"] = "launch_refused"
                task["launch_error"] = binding["error"]
                if not binding.get("execution_uncertain"):
                    task["execution_slot"]["state"] = "released"
            else:
                task["status"] = "running"
                task.update(binding)
                task["resume_count"] = 0
                task["output_history"] = []
        else:
            task["status"] = "launch_unsupported"
            if routed_from_any:
                # Byte-for-byte "any" compatibility: the pre-rotation message.
                task["launch_error"] = (
                    f"runtime '{assignee}' has no headless launch contract; "
                    "only 'muse' can be launched through the contained worker "
                    "launcher"
                )
            else:
                task["launch_error"] = (
                    f"runtime '{assignee}' has no headless launch contract; "
                    "launchable runtimes are 'muse', 'codex' and 'copilot' "
                    "(or assignee 'auto' for policy-order rotation)"
                )
        task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tasks[task_id] = task
    _save_tasks(tasks, agents_dir)

    _append_audit({
        "action": "dispatch",
        "task_id": task_id,
        "title": title.strip(),
        "assignee": assignee,
        "priority": priority,
    }, agents_dir)

    if norm_launch is not None:
        if task["status"] == "running":
            _append_audit({
                "action": "launch",
                "task_id": task_id,
                "runtime": task.get("runtime"),
                "session_id": task.get("session_id"),
                "workspace": task.get("workspace"),
                "branch": task.get("branch"),
                "lease_owner": (task.get("lease") or {}).get("owner"),
            }, agents_dir)
        else:
            _append_audit({
                "action": task["status"],
                "task_id": task_id,
                "runtime": assignee,
                "reason": task.get("launch_error", "")[:300],
            }, agents_dir)

    # Build a structured prompt that the host AI can pass to a subagent
    prompt = _build_agent_prompt(task)

    if task["status"] == "running":
        dispatch_message = (
            f"Task {task_id} running as {task.get('runtime', assignee)} session "
            f"{task.get('session_id')} ({priority})"
        )
    elif task["status"] in ("launch_refused", "launch_unsupported", "exhausted"):
        dispatch_message = f"Task {task_id} {task['status']}: {task.get('launch_error', '')}"
    else:
        dispatch_message = f"Task {task_id} dispatched to {assignee} ({priority})"

    dispatch_resp = {
        "status": task["status"],
        "task_id": task_id,
        "task": task,
        "agent_prompt": prompt,
        "message": dispatch_message,
    }
    if focus_advisory is not None:
        dispatch_resp["focus_advisory"] = focus_advisory
    if possible_duplicate is not None:
        dispatch_resp["possible_duplicate"] = possible_duplicate
    return dispatch_resp


def _build_agent_prompt(task: Dict[str, Any]) -> str:
    """Build a structured prompt for a subagent to execute the task."""
    lines = [
        f"## Agent Task: {task['id']}",
        f"**Title:** {task['title']}",
    ]
    if task.get("description"):
        lines.append(f"**Description:** {task['description']}")
    lines.append(f"**Priority:** {task['priority']}")
    lines.append(f"**Assignee:** {task['assignee']}")

    if task.get("context"):
        lines.append(f"\n**Context:**\n{task['context']}")

    if task.get("variables"):
        lines.append("\n**Variables:**")
        for key, value in task["variables"].items():
            lines.append(f"- {key}: {value}")

    if task.get("tools_needed"):
        lines.append(f"\n**Tools needed:** {', '.join(task['tools_needed'])}")

    if task.get("constraints"):
        lines.append(f"\n**Constraints:**")
        for c in task["constraints"]:
            lines.append(f"- {c}")

    if task.get("assignee") == "copilot" or task.get("runtime") == "copilot":
        lines.append(
            "\n**When done:** Return to the Delimit coordinator with a completion packet "
            f"for task_id='{task['id']}'. Include the actual inspected evidence, changed "
            "files, tests/results, commit/PR if any, and unresolved risks. Do not call "
            "`delimit_agent_complete`, do not claim durable mutation, and do not bypass "
            "coordination through shell commands. The coordinator verifies the work and "
            "calls completion."
        )
    else:
        lines.append(f"\n**When done:** Call `delimit_agent_complete` with task_id='{task['id']}' and your result.")

    return "\n".join(lines)


@_store_read_safe()
def get_agent_status(task_id: str = "") -> Dict[str, Any]:
    """Get the status of a specific task, or list all active tasks."""
    tasks = _load_tasks()

    if not task_id or not task_id.strip():
        return list_active_agents()

    task_id = task_id.strip().upper()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    return {
        "status": "ok",
        "task": tasks[task_id],
    }


@_store_read_safe()
def list_active_agents() -> Dict[str, Any]:
    """Return all tasks that are not done or failed.

    LED-5321 M5: running workers and completed/uncertain outputs awaiting
    lead disposition count as active (someone still has to act);
    cancelled/refused/unsupported/exhausted launches count as closed.
    """
    tasks = _load_tasks()
    active = {
        tid: t for tid, t in tasks.items()
        if t.get("status") in ("dispatched", "in_progress", "handed_off",
                               "running", "completed", "uncertain")
    }
    completed = {
        tid: t for tid, t in tasks.items()
        if t.get("status") in ("done", "failed", "cancelled",
                               "launch_refused", "launch_unsupported",
                               "exhausted")
    }

    return {
        "status": "ok",
        "active_count": len(active),
        "completed_count": len(completed),
        "active_tasks": list(active.values()),
        "summary": [
            {"id": t["id"], "title": t["title"], "status": t["status"],
             "assignee": t["assignee"], "priority": t["priority"]}
            for t in active.values()
        ],
    }


@_serialized_store
def complete_task(
    task_id: str,
    result: str = "",
    files_changed: Optional[List[str]] = None,
    accept_uncertain: bool = False,
    # LED-5321 M4: all optional, backward compatible. When a review
    # transcript + diff are supplied, the completion records whether the
    # transcript binds to the exact diff so a later commit cannot inherit
    # an earlier review. A worker's "done" is an assertion, never merged/
    # deployed proof on its own.
    review_transcript: str = "",
    review_diff_path: str = "",
    review_diff_text: str = "",
) -> Dict[str, Any]:
    """Mark a dispatched task as done.

    LED-5321 M5: tasks in the tracked-launch lifecycle may only close from
    ``completed``/``uncertain`` worker states, and ``uncertain`` requires an
    explicit ``accept_uncertain=True`` (the worker died mid-run, e.g. model
    stream idle timeout — the outcome is unknown). Closing a ``cancelled``,
    ``running``, ``launch_refused``, ``launch_unsupported``, or ``exhausted``
    task is an error. Legacy audit-only tasks (dispatched/in_progress/
    handed_off) close exactly as before.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()

    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    if task.get("execution_slot"):
        from ai.agent_session_slots import occupies_slot
        if occupies_slot(task):
            return {"error": "live worker may still be running; termination not confirmed; poll or cancel before closing/transferring"}

    if task["status"] == "done":
        return {"error": f"Task {task_id} is already marked done"}

    if task["status"] in _LAUNCHED_LIFECYCLE_STATUSES:
        if task["status"] == "uncertain" and not accept_uncertain:
            return {
                "error": (
                    f"Task {task_id} ended uncertain "
                    f"({task.get('uncertain_reason', 'unknown reason')}); "
                    "inspect the worker output and pass accept_uncertain=True "
                    "to close it anyway"
                )
            }
        if task["status"] not in ("completed", "uncertain"):
            return {
                "error": (
                    f"Task {task_id} is {task['status']}; only completed or "
                    "uncertain (with accept_uncertain=True) worker tasks can "
                    "be closed"
                )
            }
        if task["status"] == "uncertain":
            task["accepted_uncertain"] = True

    task["status"] = "done"
    task["result"] = result.strip()
    task["files_changed"] = files_changed or []
    task["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # LED-5321 M4: optional review binding (deterministic, no model calls).
    # Only computed when the caller supplies review inputs; otherwise the
    # completion is recorded exactly as before (assertion, not proof).
    _review_binding: Optional[Dict[str, Any]] = None
    if review_transcript or review_diff_path or review_diff_text:
        try:
            from ai.review_binding import check_review_binding

            _review_binding = check_review_binding(
                transcript_path=review_transcript or "",
                diff_path=review_diff_path or "",
                diff_text=review_diff_text or "",
            )
        except Exception as exc:
            _review_binding = {"bound": False, "reason": f"review_binding_error: {exc}"}
        task["review_binding"] = _review_binding

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "complete",
        "task_id": task_id,
        "result": result.strip()[:200],
        "files_changed": files_changed or [],
        **({"review_binding": _review_binding} if _review_binding is not None else {}),
    })

    # STR-2202: fold prompt_drift.record + checkout-lock release into the
    # completion path. Both are best-effort and FAIL-SAFE — the completion
    # result above is already committed; instrumentation errors never change it.
    _record_completion_drift(task)
    _release_checkout_claim(task)

    return {
        "status": "completed",
        "task_id": task_id,
        "task": task,
        "message": f"Task {task_id} marked as done",
    }


# ── LED-5321 M5: tracked worker question/answer + polling + cancel ─────


def _worker_identity(task: Dict[str, Any]) -> str:
    """Return the worker's attribution id (runtime:session) for a task."""
    return f"{task.get('runtime', task.get('assignee', 'worker'))}:{task.get('session_id', '?')}"


def _extract_question_block(text: str) -> str:
    """Return the first QUESTION: block in worker output, or "".

    The block starts at the first line whose stripped form begins with
    "QUESTION:" (case-insensitive) and runs to an optional END_QUESTION line
    or the end of the text. Only the first block is returned: one completion
    carries at most one bound question.
    """
    if not text:
        return ""
    lines = text.splitlines()
    start = None
    first = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.upper().startswith("QUESTION:"):
            start = i
            first = stripped[len("QUESTION:"):].strip()
            break
    if start is None:
        return ""
    buf = [first] if first else []
    for line in lines[start + 1:]:
        if line.strip().upper() == "END_QUESTION":
            break
        buf.append(line)
    return "\n".join(buf).strip()


@_serialized_store
def record_question(
    task_id: str,
    question_text: str,
    *,
    asked_by: str,
) -> Dict[str, Any]:
    """Bind a worker question to its task with a handoff receipt.

    Persists {question_id, text, asked_at, status: waiting} on the task and
    creates a handoff receipt (next_action = the question) addressed to the
    lease owner (the lead) or "owner" when no lease exists. All bindings live
    in the task store, so a fresh lead process sees them via get_agent_status.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not question_text or not question_text.strip():
        return {"error": "question_text is required"}
    if not asked_by or not asked_by.strip():
        return {"error": "asked_by is required"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    try:
        from ai.handoff_receipts import create_receipt
    except ImportError:  # pragma: no cover - flat import layout
        from handoff_receipts import create_receipt

    task = tasks[task_id]
    text = question_text.strip()
    asker = asked_by.strip()
    question_id = f"Q-{uuid.uuid4().hex[:8].upper()}"
    to_model = (task.get("lease") or {}).get("owner") or "owner"
    project_path = task.get("workspace") or os.getcwd()
    receipt = create_receipt(
        task_description=f"Worker question on {task_id} (asked by {asker}): {text[:120]}",
        next_action=text,
        from_model=asker,
        to_model=to_model,
        project_path=project_path,
        files_modified=[],
    )
    question = {
        "question_id": question_id,
        "text": text,
        "asked_at": _utcnow(),
        "asked_by": asker,
        "status": "waiting",
        "receipt_id": receipt.receipt_id,
        "receipt_project_path": project_path,
        "answer": None,
    }
    task.setdefault("questions", []).append(question)
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "question_recorded",
        "task_id": task_id,
        "question_id": question_id,
        "receipt_id": receipt.receipt_id,
        "asked_by": asker,
    })

    return {
        "status": "recorded",
        "task_id": task_id,
        "question_id": question_id,
        "receipt_id": receipt.receipt_id,
        "question": question,
        "task": task,
        "message": f"Question {question_id} on {task_id} bound to receipt {receipt.receipt_id}",
    }


def _resume_muse_worker(
    task: Dict[str, Any],
    answer_text: str,
    question_id: str,
) -> Dict[str, Any]:
    """Resume the SAME muse session with the answer as its prompt file.

    Returns task-field updates or {"error": reason}. The answer is written to
    a fresh prompt file and the launcher is invoked with the recorded session
    id plus a NEW output prefix, so the original transcript is preserved and
    the old prefix moves to output_history.
    """
    session_id = task.get("session_id") or ""
    workspace = task.get("workspace") or ""
    old_prefix = task.get("output_prefix") or ""
    if not session_id or not workspace or not old_prefix:
        return {"error": "task has no muse session binding to resume"}
    launcher = _resolve_launcher()
    launch_spec = task.get("launch") or {}
    answer_file = Path(old_prefix + f".answer-{question_id}.md")
    try:
        answer_file.write_text(answer_text)
    except OSError as e:
        return {"error": f"cannot write answer prompt file: {e}"}
    resume_spec = {
        "workspace": workspace,
        "prompt_file": str(answer_file),
        "max_steps": launch_spec.get("max_steps", _LAUNCH_DEFAULT_MAX_STEPS),
        "network": launch_spec.get("network", _LAUNCH_DEFAULT_NETWORK),
    }
    refusal = _check_launcher_preconditions(launcher, resume_spec)
    if refusal is not None:
        return {"error": refusal}
    resume_count = int(task.get("resume_count", 0) or 0) + 1
    new_prefix = f"{old_prefix}.r{resume_count}"
    cmd = [
        str(launcher),
        workspace,
        str(answer_file),
        new_prefix,
        str(resume_spec["max_steps"]),
        resume_spec["network"],
        session_id,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "launcher timed out after 120s", "execution_uncertain": True}
    except OSError as e:
        return {"error": f"launcher exec failed: {e}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1][:300] if detail else f"exit {proc.returncode}"
        pre_spawn = (_read_first_line(Path(new_prefix + ".launch-protocol")) == "1"
                     and not Path(new_prefix + ".spawn-attempted").exists())
        return {"error": f"launcher refused resume (exit {proc.returncode}): {tail}",
                "execution_uncertain": not pre_spawn}
    resumed_sid = _read_first_line(Path(new_prefix + ".session-id"))
    if resumed_sid != session_id:
        return {"error": "launcher did not resume the same session id", "execution_uncertain": True}
    now_epoch = time.time()
    lease = task.get("lease") or {}
    return {
        "output_prefix": new_prefix,
        "old_prefix": old_prefix,
        "resume_count": resume_count,
        "resumed_at": _utcnow(),
        "answer_file": str(answer_file),
        "worker_pid": _find_worker_pid(session_id),
        "lease": {
            "owner": lease.get("owner") or _lead_identity(),
            "acquired_at": _utcnow(),
            "expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(now_epoch + _WORKER_LEASE_SECONDS),
            ),
        },
    }


@_serialized_store
def answer_question(
    task_id: str,
    question_id: str,
    answer_text: str,
    *,
    answered_by: str,
    owner_backed: bool,
) -> Dict[str, Any]:
    """Answer a bound worker question and resume the muse session.

    Persists the answer (recording ``owner_backed`` exactly as given — never
    inferred; the lead may answer routine technical questions with
    owner_backed=False), acknowledges the question's handoff receipt with the
    answer, and — for muse tasks whose worker already reached a terminal
    output state (completed/uncertain) — resumes the SAME session via the
    launcher with the answer as the prompt file. No relaunch happens while
    the worker is still running (that would orphan a duplicate worker) or
    when no muse binding exists; the answer is still persisted and acked.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not question_id or not question_id.strip():
        return {"error": "question_id is required"}
    if not answer_text or not answer_text.strip():
        return {"error": "answer_text is required"}
    if not answered_by or not answered_by.strip():
        return {"error": "answered_by is required"}

    task_id = task_id.strip().upper()
    question_id = question_id.strip().upper()
    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    try:
        from ai.handoff_receipts import acknowledge_receipt
    except ImportError:  # pragma: no cover - flat import layout
        from handoff_receipts import acknowledge_receipt

    task = tasks[task_id]
    if task["status"] in ("cancelled", "done", "failed",
                          "launch_refused", "launch_unsupported", "exhausted"):
        return {"error": f"cannot answer: task {task_id} is {task['status']}"}
    question = None
    for q in task.get("questions", []):
        if q.get("question_id") == question_id:
            question = q
            break
    if question is None:
        return {"error": f"Question {question_id} not found on {task_id}"}
    if question.get("status") == "answered":
        return {"error": f"Question {question_id} is already answered"}

    from ai.agent_session_slots import admission, reserve, occupies_slot
    will_resume = bool(task.get("runtime") == "muse" and task.get("session_id")
                       and task.get("output_prefix") and task.get("workspace")
                       and task["status"] in ("completed", "uncertain"))
    if will_resume:
        if occupies_slot(task):
            return {"status": "concurrency_limited", "error": "Previous run termination not confirmed"}
        held = admission(tasks)
        if held:
            return held
        count = int(task.get("resume_count", 0) or 0) + 1
        task["execution_slot"] = reserve(f"{task['output_prefix']}.r{count}", task['session_id'])
        tasks[task_id] = task
        _save_tasks(tasks)

    answer = answer_text.strip()
    answerer = answered_by.strip()
    question["answer"] = {
        "text": answer,
        "answered_by": answerer,
        "owner_backed": bool(owner_backed),
        "answered_at": _utcnow(),
    }
    question["status"] = "answered"

    # Acknowledge the receipt best-effort: the task store is the source of
    # truth, so receipt plumbing never fails the answer itself.
    ack_status = "skipped"
    if question.get("receipt_id"):
        try:
            ack = acknowledge_receipt(
                question["receipt_id"],
                model=answerer,
                notes=answer,
                project_path=question.get("receipt_project_path") or "",
            )
            ack_status = ack.get("status", "unknown")
        except Exception as e:  # pragma: no cover — ack must never fail answer
            ack_status = f"error: {e}"

    launched_muse = bool(
        task.get("runtime") == "muse"
        and task.get("session_id")
        and task.get("output_prefix")
        and task.get("workspace")
    )
    resumed = False
    resume_error = ""
    if launched_muse and task["status"] in ("completed", "uncertain"):
        updates = _resume_muse_worker(task, answer, question_id)
        task["execution_slot"]["launch_returned"] = True
        if "error" in updates:
            resume_error = updates["error"]
            if not updates.get("execution_uncertain"):
                task["execution_slot"]["state"] = "released"
        else:
            resumed = True
            task.setdefault("run_history", []).append({
                "output_prefix": updates["old_prefix"],
                "status": task["status"],
                "final_text": task.get("final_text", ""),
                "uncertain_reason": task.get("uncertain_reason", ""),
                "observed_at": task.get("observed_at", ""),
            })
            task.setdefault("output_history", []).append(updates["old_prefix"])
            task["output_prefix"] = updates["output_prefix"]
            task["resume_count"] = updates["resume_count"]
            task["resumed_at"] = updates["resumed_at"]
            task["answer_file"] = updates["answer_file"]
            task["worker_pid"] = updates["worker_pid"]
            task["lease"] = updates["lease"]
            task["observed_rc"] = None
            task.pop("uncertain_reason", None)
            task["status"] = "running"
    elif task.get("status") == "running":
        resume_error = "worker still running; answer recorded, no relaunch"

    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "question_answered",
        "task_id": task_id,
        "question_id": question_id,
        "answered_by": answerer,
        "owner_backed": bool(owner_backed),
        "receipt_ack": ack_status,
        "resumed": resumed,
        "resume_error": resume_error[:200] if resume_error else "",
    })

    resp: Dict[str, Any] = {
        "status": "answered",
        "task_id": task_id,
        "question_id": question_id,
        "receipt_id": question.get("receipt_id", ""),
        "receipt_ack": ack_status,
        "resumed": resumed,
        "session_id": task.get("session_id", ""),
        "task": task,
    }
    if resumed:
        resp["message"] = (
            f"Question {question_id} answered; muse session "
            f"{task.get('session_id')} resumed"
        )
    elif resume_error:
        resp["resume_error"] = resume_error
        resp["message"] = f"Question {question_id} answered ({resume_error})"
    else:
        resp["message"] = f"Question {question_id} answered (no worker to resume)"
    return resp


def _read_jsonl_events(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Best-effort parse of a worker JSONL transcript into event dicts."""
    try:
        text = jsonl_path.read_text()
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _event_kind(event: Dict[str, Any]) -> str:
    """Return the terminal/lifecycle marker an event carries, or "".

    Structured match only: the marker must appear in a type/event/name/kind
    field of the event (or one level down inside data/payload).
    """
    candidates = [event]
    for nest_key in ("data", "payload"):
        nested = event.get(nest_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for obj in candidates:
        # "payload_type" is the marker field of the Muse CLI 1.2.x durable
        # event stream (top-level: {"payload_type": "run.terminal.completed",
        # "payload": {"kind": "run_terminal", "terminal": "completed",
        # "text": ...}}). Observed 2026-09-15 on task AGT-40B623AC: without it
        # every real worker exit read as "uncertain" although the terminal
        # event was present; the earlier fixtures used a synthetic "type" key.
        for key in ("type", "event", "name", "kind", "payload_type"):
            val = obj.get(key)
            if not isinstance(val, str):
                continue
            for marker in (TERMINAL_COMPLETED_EVENT, TERMINAL_FAILED_EVENT,
                           LIFECYCLE_EVENT):
                if marker in val:
                    return marker
    return ""


def _event_text(event: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    """First non-empty text among keys (plus one nested data/payload level)."""
    candidates = [event]
    for nest_key in ("data", "payload"):
        nested = event.get(nest_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for obj in candidates:
        for key in keys:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, (dict, list)) and val:
                try:
                    return json.dumps(val)[:4000]
                except (TypeError, ValueError):
                    continue
    return ""


def _parse_rc(rc_path: Path) -> Optional[int]:
    """Parse the launcher's rc sidecar (``rc=N``); None when unparsable."""
    try:
        text = rc_path.read_text().strip()
    except OSError:
        return None
    m = re.search(r"rc\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _stderr_tail(stderr_path: Path, limit: int = 500) -> str:
    """Best-effort tail of the worker stderr sidecar."""
    try:
        text = stderr_path.read_text()
    except OSError:
        return ""
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _agy_result_ok(event: Dict[str, Any]) -> bool:
    """True when an agy stream-json result event reports success.

    Structured only: any non-empty error field fails; otherwise an explicit
    success status word is required (a result without a verdict fails
    closed — the outcome is unknown, never assumed complete).
    """
    candidates = [event]
    result = event.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    for obj in candidates:
        for key in ("error", "error_message", "failure"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return False
            if isinstance(val, dict) and val:
                return False
    for obj in candidates:
        for key in ("status", "state", "verdict", "outcome"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower() in _AGY_SUCCESS_STATUSES
    return False


def _runtime_terminal_kind(event: Dict[str, Any], runtime: str) -> str:
    """Terminal marker an event carries for a runtime: completed/failed/"".

    Structured match only (exact match on a type/event/name/kind field —
    never a prose scan). Muse keeps the existing payload_type/type match;
    codex matches its exec --json stream, agy its stream-json result event,
    copilot its json stream.
    """
    if runtime == "codex":
        for key in ("type", "event", "name", "kind"):
            val = event.get(key)
            if not isinstance(val, str):
                continue
            if val == CODEX_TURN_COMPLETED:
                return "completed"
            if val in (CODEX_TURN_FAILED, CODEX_ERROR):
                return "failed"
        return ""
    if runtime == "antigravity":
        for key in ("event", "type", "name", "kind"):
            if event.get(key) == AGY_RESULT_EVENT:
                return "completed" if _agy_result_ok(event) else "failed"
        return ""
    if runtime == "copilot":
        for key in ("type", "event", "name", "kind"):
            val = event.get(key)
            if val == COPILOT_RESULT_EVENT:
                return "completed"
            # Live-verified 2026-09-22: a quota wall (HTTP 402) emits no
            # result event at all — the verdict is a session.error event
            # carrying errorType/message/statusCode/errorCode.
            if val in (COPILOT_ERROR_EVENT, COPILOT_SESSION_ERROR_EVENT):
                return "failed"
        return ""
    kind = _event_kind(event)  # muse, unchanged
    if kind == TERMINAL_COMPLETED_EVENT:
        return "completed"
    if kind == TERMINAL_FAILED_EVENT:
        return "failed"
    return ""


def _agy_final_text(event: Dict[str, Any]) -> str:
    """Structured final text from an agy result event (top level + result/)."""
    candidates = [event]
    result = event.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    for obj in candidates:
        for key in ("text", "answer", "response", "output", "message", "content"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _copilot_final_text(jsonl_path: Path, terminal_event: Dict[str, Any]) -> str:
    """Structured final text: last assistant.message content, else result text."""
    last = ""
    for event in _read_jsonl_events(jsonl_path):
        if event.get("type") != "assistant.message":
            continue
        data = event.get("data")
        if isinstance(data, dict):
            content = data.get("content")
        elif isinstance(data, str):
            content = data
        else:
            content = event.get("content")
        if isinstance(content, str) and content.strip():
            last = content.strip()
    if last:
        return last
    return _event_text(terminal_event, ("text", "message", "content", "output"))


def _codex_final_text(prefix: str, jsonl_path: Path) -> str:
    """Structured final text: the -o last-message file, else last agent text."""
    try:
        text = Path(prefix + ".last-message").read_text().strip()
        if text:
            return text
    except OSError:
        pass
    last = ""
    for event in _read_jsonl_events(jsonl_path):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            last = text.strip()
    return last


def _runtime_final_text(runtime: str, terminal_event: Dict[str, Any],
                        prefix: str, jsonl_path: Path) -> str:
    """Structured final text for a completed non-muse worker."""
    if runtime == "codex":
        return _codex_final_text(prefix, jsonl_path)
    if runtime == "antigravity":
        return _agy_final_text(terminal_event)
    if runtime == "copilot":
        return _copilot_final_text(jsonl_path, terminal_event)
    return ""


def _runtime_failure_reason(runtime: str, terminal_event: Dict[str, Any],
                            rc: Optional[int]) -> str:
    """Structured failure reason for a failed non-muse worker."""
    if runtime == "codex":
        err = terminal_event.get("error")
        if isinstance(err, dict):
            for key in ("message", "text", "detail", "details"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            try:
                return json.dumps(err)[:500]
            except (TypeError, ValueError):
                pass
        elif isinstance(err, str) and err.strip():
            return err.strip()
        message = terminal_event.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return f"codex {terminal_event.get('type', 'terminal')} event (rc={rc})"
    if runtime == "antigravity":
        candidates = [terminal_event]
        result = terminal_event.get("result")
        if isinstance(result, dict):
            candidates.append(result)
        for obj in candidates:
            for key in ("error", "error_message", "failure", "message", "reason"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, dict) and val:
                    try:
                        return json.dumps(val)[:500]
                    except (TypeError, ValueError):
                        continue
        for obj in candidates:
            status = obj.get("status")
            if isinstance(status, str) and status.strip():
                return f"agy result status {status.strip()} (rc={rc})"
        return f"agy result without a success verdict (rc={rc})"
    if runtime == "copilot":
        candidates = [terminal_event]
        data = terminal_event.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        for obj in candidates:
            for key in ("message", "error", "reason", "text"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, dict) and val:
                    try:
                        return json.dumps(val)[:500]
                    except (TypeError, ValueError):
                        continue
        if isinstance(data, dict):
            bits = [f"{key}={data[key]}" for key in
                    ("errorType", "errorCode", "statusCode")
                    if data.get(key) is not None]
            if bits:
                return f"copilot {' '.join(bits)} (rc={rc})"
        return f"copilot worker failed (rc={rc})"
    return f"worker failed (rc={rc})"


def _auto_failure_evidence(task: Dict[str, Any], reason: str) -> str:
    """Bounded failure evidence: reason + stderr tail + last-message sidecar."""
    evidence = f"{reason or ''}\n{task.get('worker_stderr_tail', '')}"
    prefix = task.get("output_prefix") or ""
    if prefix:
        try:
            evidence += "\n" + Path(str(prefix) + ".last-message").read_text()[:2000]
        except OSError:
            pass
    return evidence


def _auto_quick_quota_failure(task: Dict[str, Any], reason: str) -> bool:
    """True when a terminal failure is quick AND quota-signalled.

    The window runs from the worker's started_at to its observed_at; the
    signal is matched against the bounded failure evidence (never a full
    transcript scan).
    """
    start = _iso_epoch(task.get("started_at", ""))
    end = _iso_epoch(task.get("observed_at", "")) or time.time()
    elapsed = None if start is None else end - start
    # A skewed/future start (negative elapsed) is clock skew, not a quick
    # failure — it must never rotate.
    if elapsed is None or elapsed < 0 or elapsed > _AUTO_QUICK_FAIL_SECONDS:
        return False
    return bool(_AUTO_QUOTA_SIGNAL_RE.search(_auto_failure_evidence(task, reason)))


def _poll_rotate_next(task: Dict[str, Any], tasks: Dict[str, Any],
                      from_runtime: str, reason: str,
                      *, record_failure: bool = True) -> Dict[str, Any]:
    """Re-launch an auto task on the next untried runtime (LED-5314).

    Records the failed runtime's quota hold and flips its attempt row to
    quota_failed (skipped when re-trying a deferred rotation —
    record_failure=False — so the hold and history are written exactly once),
    then walks forward. At most one attempt per runtime. Returns a poll-style
    response: running (rotated), exhausted (no runtime left — last worker
    evidence preserved for the lead), or uncertain with a rotation/deferred
    marker when capacity is held (re-poll to retry).
    """
    from ai.agent_session_slots import admission, reserve
    task_id = task["id"]
    attempts = task.get("attempts", [])
    if record_failure:
        _dispatch_mark_quota(from_runtime, _auto_failure_evidence(task, reason))
        for entry in attempts:
            if entry.get("runtime") == from_runtime and entry.get("outcome") == "launched":
                entry["outcome"] = "quota_failed"
                entry["reason"] = (reason or "")[:300]
                entry["observed_at"] = _utcnow()
                break
        else:
            entry = _auto_attempt_entry(from_runtime, "quota_failed", reason or "")
            entry["observed_at"] = _utcnow()
            attempts.append(entry)
        task["attempts"] = attempts
    tried = {a.get("runtime") for a in attempts}
    tried.add(task.get("runtime"))
    base_prefix = task.get("run_base_prefix") or task.get("output_prefix") or ""
    from ai.provider_usage import choose_runtime
    selection = choose_runtime(task.get("priority", "P1"), task.get("task_type", ""), exclude=tried)
    recorded_skips = set()
    def record_skips_through(runtime: str) -> bool:
        if runtime not in _AUTO_POLICY_ORDER:
            attempts.append(_auto_attempt_entry(runtime, "skipped", "runtime absent from auto policy order"))
            return False
        for candidate in _AUTO_POLICY_ORDER[:_AUTO_POLICY_ORDER.index(runtime) + 1]:
            if candidate in selection["skipped"] and candidate not in recorded_skips:
                attempts.append(_auto_attempt_entry(candidate, "skipped", selection["skipped"][candidate]))
                recorded_skips.add(candidate)
        return True
    for cand in selection["ranked"]:
        if not record_skips_through(cand):
            continue
        if cand in tried:
            continue
        skip = _auto_skip_reason(cand)
        if skip is not None:
            attempts.append(_auto_attempt_entry(cand, "skipped", skip))
            tried.add(cand)
            continue
        held = admission(tasks)
        if held:
            task["attempts"] = attempts
            task["rotation"] = {
                "status": "deferred",
                "reason": "concurrency_limited; re-poll to rotate",
                "observed_at": _utcnow(),
            }
            tasks[task_id] = task
            _save_tasks(tasks)
            _append_audit({"action": "poll", "task_id": task_id,
                           "outcome": "uncertain", "rotation": "deferred",
                           "reason": (task.get("uncertain_reason") or "")[:300]})
            return {
                "status": "uncertain",
                "task_id": task_id,
                "task": task,
                "rotation": "deferred",
                "reason": task.get("uncertain_reason", ""),
                "message": (f"Worker on {task_id} ended uncertain; rotation "
                            f"deferred (concurrency limited) — re-poll to retry"),
            }
        seq = int(task.get("launch_seq", 0) or 0)
        new_prefix = str(base_prefix) + f".auto{seq}" if seq else str(base_prefix)
        task["execution_slot"] = reserve(new_prefix, str(uuid.uuid4()))
        tasks[task_id] = task
        _save_tasks(tasks)
        launch_spec = dict(task.get("launch") or {},
                           session_id=task["execution_slot"]["session_id"])
        agents_dir = _effective_agents_dir()
        if cand == "muse":
            binding = _launch_muse_worker(
                task_id=task_id, spec=launch_spec, agents_dir=agents_dir,
                output_prefix=Path(new_prefix),
            )
        else:
            binding = _launch_runtime_worker(
                runtime=cand, task_id=task_id, spec=launch_spec,
                agents_dir=agents_dir, output_prefix=Path(new_prefix),
            )
        task["execution_slot"]["launch_returned"] = True
        task["launch_seq"] = seq + 1
        if "error" in binding:
            if binding.get("execution_uncertain"):
                attempts.append(_auto_attempt_entry(
                    cand, "refused",
                    binding["error"] + " (execution uncertain; rotation stopped)"))
                task["attempts"] = attempts
                task["rotation"] = {
                    "status": "stopped",
                    "reason": f"{cand} launch uncertain; not rotating further",
                    "observed_at": _utcnow(),
                }
                tasks[task_id] = task
                _save_tasks(tasks)
                _append_audit({"action": "poll", "task_id": task_id,
                               "outcome": "uncertain", "rotation": "stopped",
                               "reason": binding["error"][:300]})
                return {
                    "status": "uncertain",
                    "task_id": task_id,
                    "task": task,
                    "rotation": "stopped",
                    "reason": task.get("uncertain_reason", ""),
                    "message": (f"Worker on {task_id} ended uncertain; rotation "
                                f"stopped ({cand} launch uncertain)"),
                }
            _dispatch_mark_refused(cand)
            task["execution_slot"]["state"] = "released"
            attempts.append(_auto_attempt_entry(cand, "refused", binding["error"]))
            tried.add(cand)
            continue
        task.setdefault("output_history", []).append(task.get("output_prefix"))
        task["output_prefix"] = binding["output_prefix"]
        task["runtime"] = binding["runtime"]
        task["session_id"] = binding["session_id"]
        task["workspace"] = binding["workspace"]
        task["branch"] = binding["branch"]
        task["started_at"] = binding["started_at"]
        task["lease"] = binding["lease"]
        task["worker_pid"] = binding.get("worker_pid")
        task["status"] = "running"
        task["resume_count"] = 0
        task["observed_rc"] = None
        task.pop("uncertain_reason", None)
        task.pop("rotation", None)
        attempts.append(_auto_attempt_entry(
            cand, "launched",
            f"worker started (session {binding['session_id']})"))
        task["attempts"] = attempts
        task["updated_at"] = _utcnow()
        tasks[task_id] = task
        _save_tasks(tasks)
        _append_audit({"action": "rotate", "task_id": task_id,
                       "from_runtime": from_runtime, "to_runtime": cand,
                       "reason": (reason or "")[:300]})
        return {
            "status": "running",
            "task_id": task_id,
            "task": task,
            "rotated_to": cand,
            "message": (f"Worker on {task_id} hit quota on {from_runtime}; "
                        f"rotated to {cand} (session {binding['session_id']})"),
        }
    record_skips_through(_AUTO_POLICY_ORDER[-1])
    attempts.append(_auto_attempt_entry(
        "claude", "skipped", "no headless launch contract; lead decides"))
    task["attempts"] = attempts
    task["status"] = "exhausted"
    task["launch_error"] = _auto_exhausted_error(attempts)
    task.pop("rotation", None)
    tasks[task_id] = task
    _save_tasks(tasks)
    _append_audit({"action": "exhausted", "task_id": task_id,
                   "reason": task["launch_error"][:300]})
    return {
        "status": "exhausted",
        "task_id": task_id,
        "task": task,
        "reason": task["launch_error"],
        "message": f"Worker on {task_id} exhausted rotation: {task['launch_error']}",
    }


@_serialized_store
def poll_worker(task_id: str) -> Dict[str, Any]:
    """Read a tracked worker's JSONL output WITHOUT inference.

    Explicit call only (the lead or a caller invokes it — no polling loop
    lives in the MCP server). Outcomes: rc file absent → ``running``;
    a terminal completed marker → ``completed`` (final text extracted, and a
    QUESTION: block — if present — is bound via record_question); a terminal
    failed marker (e.g. model stream idle timeout) → ``uncertain`` with a
    reason (NOT completed, NOT failed-task); rc present but no terminal
    event → ``uncertain`` (malformed output fails closed). LED-5314: the
    completed/failed markers are runtime-specific (muse run.terminal.*,
    codex turn.* + rc, agy stream-json result, copilot result/error), and an
    ``auto`` task whose worker fails fast with a quota signal is re-launched
    on the next runtime instead of parking uncertain.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    prefix = task.get("output_prefix") or ""
    if not prefix:
        return {
            "status": task["status"],
            "task_id": task_id,
            "task": task,
            "message": "no launched worker bound to this task",
        }
    if task.get("status") != "running":
        if (task.get("status") == "uncertain"
                and task.get("assignee") == "auto"
                and (task.get("rotation") or {}).get("status") == "deferred"):
            # A rotation deferred for capacity: re-poll retries it (the
            # failure and its hold were recorded on the first poll).
            return _poll_rotate_next(
                task, tasks, task.get("runtime") or "",
                task.get("uncertain_reason", ""), record_failure=False,
            )
        return {
            "status": task["status"],
            "task_id": task_id,
            "task": task,
            "message": f"worker is {task.get('status')}; nothing to poll",
        }

    rc_path = Path(prefix + ".rc")
    jsonl_path = Path(prefix + ".jsonl")
    stderr_path = Path(prefix + ".stderr")
    # True UTC: started_at comes from the launcher's `date -u` sidecar, so
    # observed_at must share the convention or the quick-fail elapsed skews.
    now = _utcnow()
    task["last_polled_at"] = now
    task["updated_at"] = now

    if not rc_path.exists():
        for event in _read_jsonl_events(jsonl_path):
            if _event_kind(event) == LIFECYCLE_EVENT:
                task["last_lifecycle"] = event
        tasks[task_id] = task
        _save_tasks(tasks)
        _append_audit({"action": "poll", "task_id": task_id, "outcome": "running"})
        return {
            "status": "running",
            "task_id": task_id,
            "task": task,
            "message": "worker still running (no rc file)",
        }

    rc = _parse_rc(rc_path)
    task["observed_rc"] = rc
    task["observed_at"] = now
    task["worker_stderr_tail"] = _stderr_tail(stderr_path)

    runtime = task.get("runtime") or task.get("assignee") or "muse"
    terminal_kind = ""  # "completed" | "failed"
    terminal_event: Dict[str, Any] = {}
    for event in _read_jsonl_events(jsonl_path):
        if runtime == "muse" and _event_kind(event) == LIFECYCLE_EVENT:
            task["last_lifecycle"] = event
        kind = _runtime_terminal_kind(event, runtime)
        if kind:
            terminal_kind = kind  # last terminal event wins
            terminal_event = event
    if terminal_kind == "completed" and runtime != "muse" and rc != 0:
        # A non-muse success marker with a nonzero exit disagrees with the
        # rc file — fail closed as a failure, never as completed.
        terminal_kind = "failed"
    if terminal_kind == "completed" and runtime == "codex":
        # codex exits 0 and emits turn.completed even when every file write was
        # refused by its sandbox (structured: item.completed/file_change with
        # status "failed"). A run whose writes failed is not a completed task.
        # Resolve by path and event order: a path whose LATEST file_change
        # completed without failure (a successful retry) does not count.
        latest_status: Dict[str, str] = {}
        for e in _read_jsonl_events(jsonl_path):
            item = e.get("item") if e.get("type") == "item.completed" else None
            if not isinstance(item, dict) or item.get("type") != "file_change":
                continue
            status = str(item.get("status") or "")
            for ch in (item.get("changes") or [{}]):
                path = str((ch or {}).get("path") or "")
                latest_status[path] = status
        failed_writes = [p for p, st in latest_status.items() if st == "failed"]
        if failed_writes:
            terminal_kind = "failed"
            terminal_event = {
                "type": "file_change.failed",
                "message": f"codex reported {len(failed_writes)} failed file write(s) "
                           "(sandbox refused workspace writes)",
            }

    if terminal_kind == "completed":
        if runtime == "muse":
            final_text = _event_text(
                terminal_event,
                ("text", "final_text", "final", "output", "result", "message", "content"),
            )
        else:
            final_text = _runtime_final_text(runtime, terminal_event, prefix, jsonl_path)
        task["status"] = "completed"
        task["final_text"] = final_text
        tasks[task_id] = task
        _save_tasks(tasks)
        _append_audit({"action": "poll", "task_id": task_id, "outcome": "completed"})
        resp: Dict[str, Any] = {
            "status": "completed",
            "task_id": task_id,
            "task": task,
            "final_text": final_text,
            "message": f"Worker on {task_id} completed",
        }
        question_text = _extract_question_block(final_text)
        if question_text:
            qr = record_question(task_id, question_text,
                                 asked_by=_worker_identity(task))
            if "error" not in qr:
                resp["question_id"] = qr["question_id"]
                resp["receipt_id"] = qr["receipt_id"]
                resp["task"] = _load_tasks()[task_id]
                resp["message"] += (
                    f"; question {qr['question_id']} recorded "
                    f"(receipt {qr['receipt_id']})"
                )
            else:  # pragma: no cover — record path is unit-tested directly
                resp["question_error"] = qr["error"]
        return resp

    if terminal_kind == "failed":
        if runtime == "muse":
            reason = _event_text(
                terminal_event,
                ("reason", "error", "message", "detail", "details", "text"),
            ) or f"worker failed (rc={rc})"
        else:
            reason = _runtime_failure_reason(runtime, terminal_event, rc)
    else:
        if runtime == "muse":
            reason = (
                f"worker exited (rc={rc}) without a {TERMINAL_COMPLETED_EVENT} "
                f"or {TERMINAL_FAILED_EVENT} event"
            )
        else:
            hint = _RUNTIME_TERMINAL_HINT.get(runtime, "terminal")
            reason = (f"{runtime} worker exited (rc={rc}) without a terminal "
                      f"event ({hint})")
    task["status"] = "uncertain"
    task["uncertain_reason"] = reason
    if task.get("assignee") == "auto" and _auto_quick_quota_failure(task, reason):
        # Rotation, not parking: hold the quota-dead runtime and re-launch
        # the SAME task on the next runtime (or record exhausted).
        return _poll_rotate_next(task, tasks, runtime, reason)
    tasks[task_id] = task
    _save_tasks(tasks)
    _append_audit({
        "action": "poll", "task_id": task_id, "outcome": "uncertain",
        "reason": reason[:300],
    })
    return {
        "status": "uncertain",
        "task_id": task_id,
        "task": task,
        "reason": reason,
        "message": f"Worker on {task_id} ended uncertain: {reason}",
    }


def _terminate_worker(task: Dict[str, Any]) -> Tuple[bool, str]:
    """Terminate the live worker process, if any; never raises.

    Tries the pid recorded at launch (validated: its cmdline must still
    contain the session id, so a recycled pid is never signalled), then falls
    back to pkill by session id. Returns (killed, note).
    """
    sid = task.get("session_id") or ""
    pid = task.get("worker_pid")
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = 0
        if pid_int > 0:
            try:
                with open(f"/proc/{pid_int}/cmdline", "rb") as f:
                    cmdline = f.read().decode(errors="replace")
            except OSError:
                cmdline = ""
            if sid and sid in cmdline:
                try:
                    os.kill(pid_int, signal.SIGTERM)
                except OSError:
                    pass
                else:
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        if not os.path.exists(f"/proc/{pid_int}"):
                            break
                        time.sleep(0.1)
                    else:
                        try:
                            os.kill(pid_int, signal.SIGKILL)
                        except OSError:
                            pass
                    return True, f"signalled pid {pid_int} holding session {sid[:8]}"
    if sid and shutil.which("pkill") is not None:
        try:
            proc = subprocess.run(["pkill", "-f", sid],
                                  capture_output=True, timeout=10)
            if proc.returncode == 0:
                return True, f"pkill matched session {sid[:8]}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False, "no live worker process found"


@_serialized_store
def cancel_task(task_id: str, *, reason: str) -> Dict[str, Any]:
    """Cancel a task and terminate its worker process if still alive.

    Records ``cancelled`` with the reason; cancellation and uncertain
    outcomes stay distinct (cancelled tasks can never be closed via
    complete_task — re-dispatch instead). The worker is found by the pid
    recorded at launch (cmdline-validated against the session id) with a
    pkill-by-session fallback; both are best-effort and never raise.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not reason or not reason.strip():
        return {"error": "reason is required"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    if task["status"] == "cancelled":
        return {"error": f"Task {task_id} is already cancelled"}
    if task["status"] == "done":
        return {"error": f"Task {task_id} is already done, cannot cancel"}

    killed = False
    note = "no worker process bound"
    if task.get("execution_slot"):
        from ai.agent_session_slots import reclaim
        try:
            proof = reclaim(task["execution_slot"])
        except (OSError, ValueError, KeyError) as exc:
            proof = {"released": False, "reason": f"termination evidence unavailable: {type(exc).__name__}"}
        if not proof["released"]:
            return {"status": "cancellation_held", "task_id": task_id, "reason": proof["reason"]}
        task["execution_slot"]["state"] = "released"
        task["execution_slot"]["release_evidence"] = dict(proof, operator=_lead_identity(), reason=reason.strip(), recorded_at=_utcnow())
        killed, note = proof["proof"] in ("group_terminated", "group_killed"), proof["proof"]
    elif task.get("runtime") == "muse" and task.get("session_id"):
        killed, note = _terminate_worker(task)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    task["status"] = "cancelled"
    task["cancel_reason"] = reason.strip()
    task["cancelled_at"] = now
    task["updated_at"] = now
    task["worker_killed"] = killed
    task["cancel_note"] = note
    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "cancel",
        "task_id": task_id,
        "reason": reason.strip()[:300],
        "worker_killed": killed,
        "execution_release": (task.get("execution_slot") or {}).get("release_evidence"),
    })

    _release_checkout_claim(task)

    return {
        "status": "cancelled",
        "task_id": task_id,
        "task": task,
        "worker_killed": killed,
        "message": f"Task {task_id} cancelled ({note})",
    }


@_serialized_store
def handoff_task(
    task_id: str,
    to_model: str,
    context: str = "",
) -> Dict[str, Any]:
    """Transfer a task from one AI model to another."""
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not to_model or not to_model.strip():
        return {"error": "to_model is required"}

    task_id = task_id.strip().upper()
    to_model = to_model.lower().strip()

    if to_model not in VALID_ASSIGNEES - {"any", "auto"}:
        return {"error": f"to_model must be one of: {', '.join(sorted(VALID_ASSIGNEES - {'any', 'auto'}))}"}

    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    if task.get("execution_slot"):
        from ai.agent_session_slots import occupies_slot
        if occupies_slot(task):
            return {"error": "live worker may still be running; termination not confirmed; poll or cancel before closing/transferring"}

    if task["status"] == "done":
        return {"error": f"Task {task_id} is already done, cannot hand off"}

    # LED-5321 M5: a live worker cannot be handed off by record edit — that
    # would orphan the running session. Cancel first, then re-dispatch.
    if task["status"] == "running":
        return {"error": f"Task {task_id} has a live worker (status running); cancel it before handing off"}

    from_model = task["assignee"]
    task["handoffs"].append({
        "from": from_model,
        "to": to_model,
        "context": context.strip(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    task["assignee"] = to_model
    task["status"] = "handed_off"
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Rebuild the prompt with handoff context
    if context.strip():
        task["context"] = (task.get("context", "") + "\n\n---\n**Handoff context from " +
                          from_model + ":**\n" + context.strip()).strip()

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "handoff",
        "task_id": task_id,
        "from": from_model,
        "to": to_model,
        "context_snippet": context.strip()[:200],
    })

    # STR-2202 (fold 3): release the checkout claim on handoff — ownership is
    # moving; the new owner re-claims when it re-dispatches into a checkout.
    # Best-effort, fail-safe. The claimed key is cleared so state stays honest.
    _release_checkout_claim(task)
    task["checkout_lock"] = ""
    tasks[task_id] = task
    _save_tasks(tasks)

    prompt = _build_agent_prompt(task)

    return {
        "status": "handed_off",
        "task_id": task_id,
        "from_model": from_model,
        "to_model": to_model,
        "task": task,
        "agent_prompt": prompt,
        "message": f"Task {task_id} handed off from {from_model} to {to_model}",
    }


@_store_read_safe(allowed=False, reason="Task store unavailable; constraints cannot be verified")
def enforce_constraints(task_id: str, action: str) -> Dict[str, Any]:
    """Check if an action is allowed given the task's constraints.

    Returns:
        Dict with 'allowed' (bool) and 'reason' if denied.
    """
    if not task_id or not task_id.strip():
        return {"allowed": True, "reason": "No task_id provided, no constraints to check"}

    task_id = task_id.strip().upper()
    tasks = _load_tasks()

    if task_id not in tasks:
        return {"allowed": True, "reason": f"Task {task_id} not found, defaulting to allow"}

    task = tasks[task_id]
    constraints = task.get("constraints", [])

    if not constraints:
        return {"allowed": True, "reason": "No constraints on this task"}

    action_lower = action.lower().strip() if action else ""

    # Check each constraint against the action
    for constraint in constraints:
        c = constraint.lower().strip()

        if c == "read-only":
            write_keywords = ["write", "edit", "create", "delete", "modify", "deploy", "push", "commit"]
            if any(kw in action_lower for kw in write_keywords):
                return {
                    "allowed": False,
                    "reason": f"Constraint 'read-only' blocks action: {action}",
                    "constraint": constraint,
                }

        elif c == "no-deploy" or c == "no-deploys":
            deploy_keywords = ["deploy", "publish", "release", "rollback"]
            if any(kw in action_lower for kw in deploy_keywords):
                return {
                    "allowed": False,
                    "reason": f"Constraint '{constraint}' blocks action: {action}",
                    "constraint": constraint,
                }

        elif c == "must-lint" or c == "must_lint":
            # This is an affirmative constraint — doesn't block, just flags
            pass

        elif c.startswith("no-"):
            # Generic "no-X" constraint
            blocked = c[3:]
            if blocked in action_lower:
                return {
                    "allowed": False,
                    "reason": f"Constraint '{constraint}' blocks action: {action}",
                    "constraint": constraint,
                }

    return {"allowed": True, "reason": "All constraints passed"}


@_serialized_store
def link_ledger_item(task_id: str, ledger_item_id: str) -> Dict[str, Any]:
    """Link a dispatched agent task to a ledger item (LED-xxx or STR-xxx).

    This creates a bidirectional relationship so the dashboard can show
    which agent is working on which ledger item.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    if not ledger_item_id or not ledger_item_id.strip():
        return {"error": "ledger_item_id is required"}

    task_id = task_id.strip().upper()
    ledger_item_id = ledger_item_id.strip().upper()

    tasks = _load_tasks()
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}

    task = tasks[task_id]
    linked = task.get("linked_ledger_items", [])
    if ledger_item_id not in linked:
        linked.append(ledger_item_id)
    task["linked_ledger_items"] = linked
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tasks[task_id] = task
    _save_tasks(tasks)

    _append_audit({
        "action": "link_ledger",
        "task_id": task_id,
        "ledger_item_id": ledger_item_id,
    })

    return {
        "status": "linked",
        "task_id": task_id,
        "ledger_item_id": ledger_item_id,
        "all_linked": linked,
        "message": f"Task {task_id} linked to {ledger_item_id}",
    }


# STR-2202 / doc 25 §8 item #5: statuses eligible for stale auto-close. Only
# "dispatched" (an intent nothing ever picked up) closes by default — an
# in_progress/handed_off task may have an active worker, so it is left alone
# unless the caller explicitly widens the set.
_AUTO_CLOSE_DEFAULT_STATUSES = frozenset({"dispatched"})

# Default staleness horizon for the groom auto-close (hours). Overridable via
# DELIMIT_DISPATCH_STALE_HOURS. 0 disables auto-close entirely (fail-safe).
_DISPATCH_STALE_HOURS_DEFAULT = 48


def _dispatch_stale_hours_default() -> int:
    raw = os.environ.get("DELIMIT_DISPATCH_STALE_HOURS", "").strip()
    if not raw:
        return _DISPATCH_STALE_HOURS_DEFAULT
    try:
        val = int(raw)
        return val if val >= 0 else _DISPATCH_STALE_HOURS_DEFAULT
    except ValueError:
        return _DISPATCH_STALE_HOURS_DEFAULT


@_serialized_store
def auto_close_stale_dispatches(
    threshold_hours: Optional[int] = None,
    dry_run: bool = True,
    statuses: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Auto-close dispatch intents that have gone stale (doc 25 §8 item #5).

    Wired into the existing groom pass so the swamp of never-consumed dispatch
    intents (~26 >24h at design time) drains automatically instead of piling
    up in the Board Report's exceptions queue.

    Discipline (mirrors ``ledger_manager.auto_cancel_stale``):
      * ``dry_run=True`` (default) returns a plan and writes nothing; the
        caller passes ``dry_run=False`` explicitly to apply.
      * Pool-lifecycle task types (``_DLQ_EXEMPT_TASK_TYPES`` — e.g.
        ``outreach_substantive``) are NEVER closed: they live at
        ``dispatched`` as a target pool by design, not as a stuck backlog.
      * Only ``statuses`` (default ``{"dispatched"}``) are eligible; an
        in_progress/handed_off task may have an active worker.
      * ``threshold_hours == 0`` disables the sweep (fail-safe no-op).
      * Applied closes set ``status="failed"`` with an ``auto_closed`` marker
        + ``close_reason`` and an audit entry, so the action is fully
        traceable and reversible (re-dispatch restores the work).

    Returns a summary dict; never raises into the groom cron.
    """
    if threshold_hours is None:
        threshold_hours = _dispatch_stale_hours_default()
    statuses = statuses or _AUTO_CLOSE_DEFAULT_STATUSES

    if threshold_hours == 0:
        return {
            "dry_run": dry_run,
            "threshold_hours": 0,
            "would_close" if dry_run else "closed": [],
            "summary": {"scanned": 0, "stale": 0, "closed": 0,
                        "note": "DELIMIT_DISPATCH_STALE_HOURS=0 disables auto-close"},
        }

    base = _effective_agents_dir()
    tasks = _load_tasks(base)
    now = time.time()
    cutoff = now - threshold_hours * 3600

    stale: List[Dict[str, Any]] = []
    for tid, t in tasks.items():
        if t.get("execution_slot"):
            continue
        if t.get("status") not in statuses:
            continue
        if t.get("task_type") in _DLQ_EXEMPT_TASK_TYPES:
            continue
        ref_ts = t.get("updated_at") or t.get("created_at") or ""
        epoch = _iso_epoch(ref_ts)
        if epoch is None or epoch >= cutoff:
            continue
        stale.append(t)

    records = [
        {"id": t.get("id"), "title": (t.get("title") or "")[:80],
         "status": t.get("status"), "task_type": t.get("task_type", ""),
         "last_seen": t.get("updated_at") or t.get("created_at")}
        for t in stale
    ]

    key = "would_close" if dry_run else "closed"
    if dry_run or not stale:
        return {
            "dry_run": dry_run,
            "threshold_hours": threshold_hours,
            key: records,
            "summary": {"scanned": len(tasks), "stale": len(stale),
                        "closed": 0, "truncated": False},
        }

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = f"auto-closed: stale dispatch intent > {threshold_hours}h"
    closed = 0
    for t in stale:
        t["status"] = "failed"
        t["auto_closed"] = True
        t["close_reason"] = reason
        t["closed_at"] = stamp
        t["updated_at"] = stamp
        tasks[t["id"]] = t
        closed += 1
    _save_tasks(tasks, base)
    _append_audit({
        "action": "auto_close_stale",
        "closed": [r["id"] for r in records],
        "threshold_hours": threshold_hours,
    }, base)

    return {
        "dry_run": False,
        "threshold_hours": threshold_hours,
        "closed": records,
        "summary": {"scanned": len(tasks), "stale": len(stale),
                    "closed": closed, "truncated": False},
    }


@_store_read_safe()
def get_agent_dashboard() -> Dict[str, Any]:
    """Return a full dashboard view of all agent activity.

    Groups tasks by status and assignee, includes audit trail summary,
    and handoff history.
    """
    tasks = _load_tasks()

    by_assignee: Dict[str, List[Dict]] = {}
    by_status: Dict[str, int] = {}
    handoff_count = 0

    for t in tasks.values():
        assignee = t.get("assignee", "unknown")
        status = t.get("status", "unknown")
        by_assignee.setdefault(assignee, []).append(t)
        by_status[status] = by_status.get(status, 0) + 1
        handoff_count += len(t.get("handoffs", []))

    # Recent audit entries
    recent_audit: List[Dict] = []
    if AUDIT_FILE.exists():
        try:
            lines = AUDIT_FILE.read_text().strip().split("\n")
            for line in lines[-20:]:
                try:
                    recent_audit.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    return {
        "status": "ok",
        "total_tasks": len(tasks),
        "by_status": by_status,
        "by_assignee": {
            model: {
                "total": len(model_tasks),
                "active": sum(1 for t in model_tasks if t["status"] in ("dispatched", "in_progress", "handed_off", "running", "completed", "uncertain")),
                "done": sum(1 for t in model_tasks if t["status"] == "done"),
                "tasks": [
                    {"id": t["id"], "title": t["title"], "status": t["status"],
                     "priority": t.get("priority", "P1"),
                     "linked_ledger": t.get("linked_ledger_items", []),
                     "task_type": t.get("task_type", ""),
                     "venture": t.get("venture", ""),
                     "variables": t.get("variables", {})}
                    for t in model_tasks
                ],
            }
            for model, model_tasks in by_assignee.items()
        },
        "handoff_count": handoff_count,
        "recent_audit": recent_audit,
    }
