"""LED-193 pre-push gate.

The PRODUCT INVARIANT: daemon AUTHORS code, NEVER merges. A daemon-
authored PR is opened only when ALL of the following local checks pass:

    1. Repo's existing pre-push hook succeeds (if installed).
       The branch's commit must already pass the hook before push.
    2. ``delimit_security_audit`` reports no NEW critical/high vulns.
    3. ``delimit_test_smoke`` passes when tests exist (no_framework
       counts as a pass — we don't block on the absence of tests).
    4. ``delimit_lint`` passes when the diff includes a spec change.

Any failure → do NOT open PR. Mark item ``failed``, audit, exit.

Self-eat dog food: each pass through this gate IS a Delimit attestation
of the daemon's own authorship — the panel called this load-bearing.
The gate output is JSON-serializable and lands in the audit log under
``gate_results`` so the eventual PR description can link the local
attestation hash.

Implementation notes:
    - We import the real backends directly (``tools_real.test_smoke``,
      ``tools_infra.security_audit``). These are the same code paths
      ``delimit_test_smoke`` and ``delimit_security_audit`` MCP tools
      drive, so the local gate matches the merge gate.
    - Lint runs only when the diff contains an OpenAPI spec file
      (``.yaml``/``.yml``/``.json`` whose content matches openapi
      heuristics). For the MVP profiles (format/lockfile/typo) lint
      will almost never trigger — but the wiring is here for when
      ``bounded_patch`` graduates.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.led193_daemon.gate")


# ── Result containers ──────────────────────────────────────────────────


@dataclass
class GateResult:
    ok: bool = True
    reason: str = ""
    security_audit: Dict[str, Any] = field(default_factory=dict)
    test_smoke: Dict[str, Any] = field(default_factory=dict)
    lint: Dict[str, Any] = field(default_factory=dict)
    pre_push_hook: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Individual gates ───────────────────────────────────────────────────


def _run_security_audit(repo_path: Path) -> Dict[str, Any]:
    """Run delimit_security_audit. Pass iff no critical/high vulns."""
    try:
        from ai.backends import tools_infra  # local import to avoid heavy import on cold path
    except Exception as exc:  # pragma: no cover — gateway always has it
        return {"ok": False, "error": f"backend_unavailable: {exc}"}
    try:
        result = tools_infra.security_audit(target=str(repo_path))
    except Exception as exc:
        return {"ok": False, "error": f"audit_raised: {exc}"}
    severity_counts = result.get("severity_counts") or {}
    critical = int(severity_counts.get("critical") or 0)
    high = int(severity_counts.get("high") or 0)
    ok = (critical == 0 and high == 0)
    return {
        "ok": ok,
        "critical": critical,
        "high": high,
        "tools_used": result.get("tools_used") or [],
        "severity_counts": severity_counts,
    }


def _run_test_smoke(repo_path: Path) -> Dict[str, Any]:
    """Run delimit_test_smoke. Absent test framework = PASS (we don't
    block on the absence of tests)."""
    try:
        from ai.backends import tools_real
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"backend_unavailable: {exc}"}
    try:
        result = tools_real.test_smoke(project_path=str(repo_path))
    except Exception as exc:
        return {"ok": False, "error": f"smoke_raised: {exc}"}
    status = result.get("status", "")
    if status == "no_framework":
        return {"ok": True, "status": "no_framework"}
    if status == "error":
        return {"ok": False, "status": "error", "error": result.get("error", "")}
    failed = int(result.get("failed") or 0)
    errors = int(result.get("errors") or 0)
    passed = int(result.get("passed") or 0)
    ok = (failed == 0 and errors == 0)
    return {
        "ok": ok,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "framework": result.get("framework", ""),
    }


def _changed_files(repo_path: Path, runner=None) -> List[str]:
    """Return the file paths changed in the staged commit (HEAD vs
    HEAD~1) or in the working tree as a fallback. Never raises — returns
    [] on any error so the caller can decide."""
    cmds = [
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        ["git", "diff", "--name-only", "HEAD"],
    ]
    for cmd in cmds:
        try:
            if runner is not None:
                proc = runner(cmd, cwd=str(repo_path))
                stdout = getattr(proc, "stdout", "") or ""
                rc = getattr(proc, "returncode", 0)
            else:
                p = subprocess.run(
                    cmd, cwd=str(repo_path), capture_output=True,
                    text=True, timeout=10, check=False,
                )
                stdout, rc = p.stdout, p.returncode
            if rc == 0 and stdout.strip():
                return [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        except (subprocess.TimeoutExpired, OSError):
            continue
    return []


def _looks_like_spec(path: Path) -> bool:
    """Heuristic: file is OpenAPI-ish — yaml/yml/json AND content
    contains ``openapi:``/``swagger:`` near the top."""
    if path.suffix.lower() not in (".yaml", ".yml", ".json"):
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    sample = head.lower()
    return ("openapi:" in sample) or ('"openapi"' in sample) or ("swagger:" in sample)


def _run_lint_if_applicable(
    repo_path: Path,
    *,
    runner=None,
) -> Dict[str, Any]:
    """Run delimit_lint when the diff contains a spec change.

    For MVP profiles this is almost always a no-op (format/lockfile/typo
    don't touch specs). The wiring is here so when ``bounded_patch``
    graduates, the gate is already complete.
    """
    changed = _changed_files(repo_path, runner=runner)
    spec_files = []
    for rel in changed:
        p = repo_path / rel
        if p.exists() and _looks_like_spec(p):
            spec_files.append(rel)
    if not spec_files:
        return {"ok": True, "applicable": False, "reason": "no_spec_change"}
    # We need a baseline to lint against. The daemon has no way to
    # reliably reconstruct one in MVP without a checkout dance. For
    # safety we DEFER: if a spec change shows up, we fail-closed and
    # surface a "lint_required_baseline_unknown" reason. Founder reviews
    # and either runs lint manually or graduates the daemon.
    return {
        "ok": False,
        "applicable": True,
        "reason": "lint_required_baseline_unknown",
        "spec_files": spec_files,
    }


def _run_pre_push_hook(repo_path: Path, runner=None) -> Dict[str, Any]:
    """Run the repo's pre-push hook directly if installed.

    Pre-push hooks are NOT exec'd by ``git`` until ``git push`` runs.
    We invoke the hook script ourselves with empty stdin so the result
    matches what ``git push`` would observe — surface failures BEFORE
    we hit the network.

    Spec-required check (LED-129 enforced via ``delimit setup hooks``).
    Absent hook = pass (we don't enforce hook presence on third-party
    repos in MVP).
    """
    hook = repo_path / ".git" / "hooks" / "pre-push"
    if not hook.exists():
        return {"ok": True, "ran": False, "reason": "hook_absent"}
    if not hook.is_file():
        return {"ok": True, "ran": False, "reason": "hook_not_file"}
    cmd = [str(hook), "origin", "git@example.com:owner/repo.git"]
    try:
        if runner is not None:
            proc = runner(cmd, cwd=str(repo_path))
            stdout = getattr(proc, "stdout", "") or ""
            stderr = getattr(proc, "stderr", "") or ""
            rc = getattr(proc, "returncode", 1)
        else:
            p = subprocess.run(
                cmd,
                cwd=str(repo_path),
                input="",  # no refs being pushed in this dry run
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            stdout, stderr, rc = p.stdout, p.stderr, p.returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "ran": True, "reason": f"hook_failed: {exc}"}
    return {
        "ok": rc == 0,
        "ran": True,
        "returncode": rc,
        "stdout_tail": (stdout or "")[-500:],
        "stderr_tail": (stderr or "")[-500:],
    }


# ── Public API ─────────────────────────────────────────────────────────


def run_pre_push_gate(
    repo_path: Path,
    *,
    runner=None,
    skip_pre_push_hook: bool = False,
    skip_lint: bool = False,
) -> GateResult:
    """Run all gates against ``repo_path``. Return ``GateResult``.

    Order is intentional: cheapest first, fail-fast.

        1. pre-push hook    (subprocess, repo-local; skipped if absent)
        2. test_smoke       (fast on small repos)
        3. security_audit   (slower; pip-audit / npm audit network calls)
        4. lint             (only if a spec changed)

    Any failure short-circuits — we don't run downstream checks because
    a single failure is enough to refuse the push.

    Test hooks:
        runner: subprocess shim used by the pre-push hook + git diff
            invocations. The Python-import-driven test_smoke / audit /
            lint backends are mocked separately by patching the modules.
        skip_pre_push_hook: bypass when the test fixture didn't install
            a hook.
        skip_lint: bypass for tests that don't care about spec-change
            detection (most of them).
    """
    result = GateResult()

    # 1. Pre-push hook
    if skip_pre_push_hook:
        result.pre_push_hook = {"ok": True, "ran": False, "reason": "skipped"}
    else:
        result.pre_push_hook = _run_pre_push_hook(repo_path, runner=runner)
    if not result.pre_push_hook.get("ok"):
        result.ok = False
        result.reason = "pre_push_hook_failed"
        return result

    # 2. test_smoke
    result.test_smoke = _run_test_smoke(repo_path)
    if not result.test_smoke.get("ok"):
        result.ok = False
        result.reason = "test_smoke_failed"
        return result

    # 3. security_audit
    result.security_audit = _run_security_audit(repo_path)
    if not result.security_audit.get("ok"):
        result.ok = False
        result.reason = "security_audit_failed"
        return result

    # 4. lint (only if spec changes detected)
    if skip_lint:
        result.lint = {"ok": True, "applicable": False, "reason": "skipped"}
    else:
        result.lint = _run_lint_if_applicable(repo_path, runner=runner)
    if not result.lint.get("ok"):
        result.ok = False
        result.reason = "lint_failed"
        return result

    return result
