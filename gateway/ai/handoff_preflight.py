"""LED-1710 — fail-closed cross-agent handoff pre-flight validator (Phase 1).

When sessions switch between coding agents (claude -> antigravity -> codex ->
gemini), the next agent can inherit *corrupted* repository / environment state:

  * ``core.bare=true`` (a repo flipped into bare mode, so every working-tree
    git operation silently misbehaves),
  * a junk ``test@*`` / empty git identity (commits attributed to a non-person,
    which trips Vercel's commit-author gate and pollutes history),
  * leaked ``GIT_*`` env vars (``GIT_DIR`` / ``GIT_WORK_TREE`` / ... pointing a
    subprocess at the *wrong* repo across the handoff),
  * a stale ``<gitdir>/index.lock`` left behind by a killed git process,
  * no recent ``~/.delimit/.last_capture`` stamp (so the next agent has no
    fresh context to revive — it starts blind).

This module is the **validator only** (Phase 1). It is PURE and READ-ONLY: it
INSPECTS git config / process env / the capture stamp and REPORTS a verdict
with remediation strings. It performs NO writes, NO env mutation, and NO git
config changes — running it can never itself corrupt the state it is checking.

Phase 2 (auto-remediation + wiring into the live agent-switch / Stop hook /
Auto-Phoenix path) is intentionally NOT built here — it is held for founder
review. Nothing in this file is wired into any live handoff path.

Design contract:
  * ``preflight_check`` never raises (best-effort): a check that errors becomes
    ``ok=False`` with the error in ``detail`` rather than propagating.
  * Severity ``critical`` means a corrupted-state class that WILL mislead the
    next agent (bare repo, bad identity); ANY failing critical check sets the
    overall verdict ``ok=False`` (fail-closed).
  * Severity ``warn`` means a likely-but-not-certain hazard (env leak, stale
    lock, stale context) that should be surfaced but does not by itself block.

Keep this dependency-free beyond stdlib + the sibling ``last_capture`` helper.
"""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── Invariant constants ────────────────────────────────────────────────

# Junk git identities are NEVER valid — the ``test@*`` / example / localhost
# patterns that fixture/CI setups leave behind (corrupted state per the
# 2026-04-07 + 2026-05-18 incident classes). An optional strict allowlist can
# be configured per-environment via DELIMIT_GIT_IDENTITIES (comma-separated) —
# recommended for org accounts; absent it, any real-looking committer passes.
# No specific identity is hardcoded in shipped source (SHIFT-1 anonymity).
_JUNK_EMAIL_RE = re.compile(
    r"(^test@|@example\.(com|org)\b|@(test\b|localhost\b)|\.local$|\.test$)", re.I
)


def _permitted_git_emails():
    raw = os.environ.get("DELIMIT_GIT_IDENTITIES", "")
    return tuple(e.strip() for e in raw.split(",") if e.strip())


def _is_junk_email(email):
    return (not email) or bool(_JUNK_EMAIL_RE.search(email))

# GIT_* env vars that, if leaked across a handoff, would silently redirect the
# next agent's git subprocesses at the wrong object store / work tree / index.
LEAKABLE_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)

# How fresh the session-context stamp must be for the next agent to have
# something useful to revive. 24h is generous: older than that and the next
# agent effectively starts blind (the 2026-05-27 "revive blind" incident class).
CAPTURE_FRESHNESS_SECONDS = 24 * 60 * 60


# ─── Repo resolution ────────────────────────────────────────────────────


def _resolve_repo(project_path: str = "") -> str:
    """Resolve the repository path to inspect (read-only, no side effects).

    Priority: explicit ``project_path`` arg -> the LED-1715 gateway resolver
    (``ai/_paths.gateway_repo``) -> the current working directory. Never
    raises; falls back to cwd on any error.
    """
    if project_path and str(project_path).strip():
        return str(project_path).strip()
    try:
        from ai._paths import gateway_repo  # type: ignore
        return gateway_repo()
    except Exception:
        try:
            from _paths import gateway_repo  # type: ignore
            return gateway_repo()
        except Exception:
            return os.getcwd()


def _hermetic_git_env() -> Dict[str, str]:
    """A copy of the process env with all leakable GIT_* vars stripped.

    Used for THIS validator's own git subprocesses so that a leaked
    ``GIT_DIR`` (etc.) in the ambient env — exactly the corruption class we are
    detecting — cannot pollute our reads. We detect the leak via
    ``os.environ`` directly (see ``_check_no_git_env_leak``) and read git from
    a clean env so the leak does not skew the *other* checks. This never
    mutates ``os.environ``; it returns a fresh dict.
    """
    env = dict(os.environ)
    for var in LEAKABLE_GIT_ENV_VARS:
        env.pop(var, None)
    return env


def _git(repo: str, args: List[str], timeout: int = 5) -> subprocess.CompletedProcess:
    """Run ``git -C <repo> <args>`` in a hermetic env. Read-only by contract.

    The caller is responsible for passing only read-only git subcommands
    (``config``, ``rev-parse``, ``--git-dir``). cwd is pinned to ``repo`` and
    the env has leakable GIT_* vars stripped so the check is self-contained.
    """
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=repo,
        env=_hermetic_git_env(),
    )


def _check(name: str, ok: bool, severity: str, detail: str, remediation: str) -> Dict[str, Any]:
    """Build one normalized check record."""
    return {
        "name": name,
        "ok": bool(ok),
        "severity": severity,
        "detail": detail,
        "remediation": remediation if not ok else "",
    }


# ─── Individual invariant checks (each best-effort, never raises) ─────────


def _check_git_identity(repo: str) -> Dict[str, Any]:
    """CRITICAL: configured user.email is a real committer (not junk), and —
    if DELIMIT_GIT_IDENTITIES is set — one of the permitted identities."""
    remediate = "set a real committer: git -C <repo> config user.email <you@org>"
    try:
        r = _git(repo, ["config", "user.email"])
        email = (r.stdout or "").strip()
        if _is_junk_email(email):
            detail = ("git user.email is empty/unset for this repo" if not email
                      else f"user.email={email!r} looks like a junk/fixture identity")
            return _check("git_identity", False, "critical", detail, remediate)
        allow = _permitted_git_emails()
        if allow and email not in allow:
            return _check(
                "git_identity", False, "critical",
                f"user.email={email!r} is not in DELIMIT_GIT_IDENTITIES",
                "git -C <repo> config user.email <one of DELIMIT_GIT_IDENTITIES>",
            )
        return _check("git_identity", True, "critical", f"user.email={email}", "")
    except Exception as e:  # pragma: no cover - defensive
        return _check("git_identity", False, "critical",
                      f"could not read git user.email: {e}", remediate)


def _check_not_bare(repo: str) -> Dict[str, Any]:
    """CRITICAL: the repo is NOT in bare mode (core.bare=false)."""
    try:
        r = _git(repo, ["rev-parse", "--is-bare-repository"])
        val = (r.stdout or "").strip().lower()
        if r.returncode != 0:
            return _check(
                "not_bare", False, "critical",
                f"git rev-parse failed (rc={r.returncode}): {(r.stderr or '').strip()}",
                "verify the path is a valid git work tree; "
                "git -C <repo> config core.bare false",
            )
        if val == "false":
            return _check("not_bare", True, "critical", "core.bare=false (work tree)", "")
        return _check(
            "not_bare", False, "critical",
            f"repository reports is-bare-repository={val!r}",
            "git -C <repo> config core.bare false",
        )
    except Exception as e:  # pragma: no cover - defensive
        return _check(
            "not_bare", False, "critical",
            f"could not determine bare status: {e}",
            "git -C <repo> config core.bare false",
        )


def _check_no_git_env_leak() -> Dict[str, Any]:
    """WARN: no leakable GIT_* var is set in the current process env."""
    try:
        leaked = [v for v in LEAKABLE_GIT_ENV_VARS if os.environ.get(v)]
        if not leaked:
            return _check("no_git_env_leak", True, "warn", "no GIT_* env vars set", "")
        return _check(
            "no_git_env_leak", False, "warn",
            "leaked GIT_* env var(s) would redirect the next agent's git "
            f"subprocesses: {', '.join(leaked)}",
            "unset " + " ".join(leaked) + " before the handoff",
        )
    except Exception as e:  # pragma: no cover - defensive
        return _check(
            "no_git_env_leak", False, "warn",
            f"could not inspect process env: {e}",
            "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR",
        )


def _gitdir(repo: str) -> Optional[Path]:
    """Resolve the absolute .git dir for ``repo`` (read-only). None on error."""
    try:
        r = _git(repo, ["rev-parse", "--git-dir"])
        if r.returncode != 0:
            return None
        raw = (r.stdout or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else (Path(repo) / p)
    except Exception:
        return None


def _check_no_stale_index_lock(repo: str) -> Dict[str, Any]:
    """WARN: no ``<gitdir>/index.lock`` is present."""
    try:
        gd = _gitdir(repo)
        if gd is None:
            return _check(
                "no_stale_index_lock", False, "warn",
                "could not resolve the .git directory to check for index.lock",
                "verify the repo path is valid",
            )
        lock = gd / "index.lock"
        if not lock.exists():
            return _check("no_stale_index_lock", True, "warn", "no index.lock present", "")
        return _check(
            "no_stale_index_lock", False, "warn",
            f"stale lock present: {lock} (a killed git process likely left it)",
            f"verify no git process is running, then rm {lock}",
        )
    except Exception as e:  # pragma: no cover - defensive
        return _check(
            "no_stale_index_lock", False, "warn",
            f"could not check for index.lock: {e}",
            "inspect <gitdir>/index.lock manually",
        )


def _check_handoff_freshness(now: Optional[float] = None) -> Dict[str, Any]:
    """WARN: a recent ``~/.delimit/.last_capture`` stamp exists."""
    try:
        try:
            from ai.last_capture import read_last_capture  # type: ignore
        except Exception:
            from last_capture import read_last_capture  # type: ignore
        stamp = read_last_capture()
        if not stamp:
            return _check(
                "handoff_freshness", False, "warn",
                "no ~/.delimit/.last_capture stamp — the next agent has no "
                "fresh context to revive (would start blind)",
                "run delimit_soul_capture (or delimit_session_handoff) before the handoff",
            )
        try:
            ts = float(stamp.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0.0
        cur = float(now) if now is not None else time.time()
        age = cur - ts
        if 0 <= age <= CAPTURE_FRESHNESS_SECONDS:
            hrs = age / 3600.0
            return _check(
                "handoff_freshness", True, "warn",
                f"last_capture is fresh ({hrs:.1f}h old, source={stamp.get('source', '?')})",
                "",
            )
        hrs = age / 3600.0
        return _check(
            "handoff_freshness", False, "warn",
            f"last_capture is stale ({hrs:.1f}h old; threshold "
            f"{CAPTURE_FRESHNESS_SECONDS / 3600:.0f}h) — next agent's context may be outdated",
            "run delimit_soul_capture to refresh session context before the handoff",
        )
    except Exception as e:  # pragma: no cover - defensive
        return _check(
            "handoff_freshness", False, "warn",
            f"could not read the capture stamp: {e}",
            "run delimit_soul_capture before the handoff",
        )


# ─── Public entrypoint ──────────────────────────────────────────────────


def preflight_check(project_path: str = "") -> Dict[str, Any]:
    """Validate cross-agent handoff invariants (read-only, fail-closed).

    Inspects the resolved repository's git config + the process env + the
    ``~/.delimit/.last_capture`` stamp and returns a verdict. Performs NO
    writes and NO mutations — running it cannot corrupt the state it checks.

    Args:
        project_path: optional repository path to inspect. Empty resolves via
            the LED-1715 gateway resolver, then cwd.

    Returns:
        ``{"ok": bool, "checks": [...], "summary": str, "project_path": str}``.
        ``ok`` is False if ANY ``critical`` check fails (fail-closed). Each
        check is ``{name, ok, severity, detail, remediation}``.
    """
    repo = _resolve_repo(project_path)

    checks: List[Dict[str, Any]] = [
        _check_git_identity(repo),
        _check_not_bare(repo),
        _check_no_git_env_leak(),
        _check_no_stale_index_lock(repo),
        _check_handoff_freshness(),
    ]

    failed_critical = [c for c in checks if c["severity"] == "critical" and not c["ok"]]
    failed_warn = [c for c in checks if c["severity"] == "warn" and not c["ok"]]
    ok = len(failed_critical) == 0

    if ok and not failed_warn:
        summary = "PASS: all handoff invariants hold — safe to switch agents."
    elif ok:
        names = ", ".join(c["name"] for c in failed_warn)
        summary = (
            f"PASS (with {len(failed_warn)} warning(s)): no critical corruption, "
            f"but review: {names}."
        )
    else:
        names = ", ".join(c["name"] for c in failed_critical)
        summary = (
            f"FAIL (fail-closed): {len(failed_critical)} critical invariant(s) "
            f"violated — the next agent would inherit corrupted state: {names}. "
            "Remediate before handoff."
        )

    return {
        "ok": ok,
        "checks": checks,
        "summary": summary,
        "project_path": repo,
    }
