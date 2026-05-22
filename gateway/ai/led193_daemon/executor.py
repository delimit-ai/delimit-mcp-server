"""LED-193 profile executors.

Three deterministic Class A profiles:
    - format_fix         — run language formatter, commit if changed
    - lockfile_refresh   — refresh package lockfile, commit if changed
    - docs_typo          — exact-string replacement across a file glob

NO LLM. NO judgment. Each profile is a pure transformation that either
produces a diff or doesn't. If it doesn't, the executor returns
``noop`` and no PR is opened.

Hard sandbox enforcement (per spec):
    - Branch must start with ``auto/`` — we generate it; never accept
      a caller-provided name.
    - NEVER push to ``main``/``master``/``trunk``/``develop``. This is
      a defensive check on the branch name; the executor will refuse
      to push any branch whose name doesn't start with the ``auto/``
      prefix.
    - NEVER ``--no-verify``. The push command is constructed without it.
    - NEVER force-push. The push command is constructed without ``-f``
      / ``--force``.
    - Filesystem write blacklist: we refuse to operate on a repo path
      that resolves under ``~/.delimit/secrets/``, ``~/.config/``,
      ``/etc/``, ``/root/.ssh/``.

NB: the executor does not itself spawn a subagent. Per spec the
deterministic profiles run in-process (no LLM). The Agent-tool
``isolation: "worktree"`` recommendation in the spec applies to the
Class C ``bounded_patch`` profile that comes later. For MVP, the
sandbox is "this Python process running these specific subprocess
commands inside a worktree-safe target repo".
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.led193_daemon.executor")


# ── Sandbox-violation constants ────────────────────────────────────────

FORBIDDEN_BASE_BRANCHES = {"main", "master", "trunk", "develop", "release"}
FORBIDDEN_PATH_PREFIXES = (
    str(Path.home() / ".delimit" / "secrets"),
    str(Path.home() / ".config"),
    "/etc",
    "/root/.ssh",
)
DANGEROUS_REPLACE_PATTERNS = [
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM", re.IGNORECASE),
    re.compile(r";\s*--"),
    re.compile(r"<script\b", re.IGNORECASE),
    re.compile(r"\$\{\s*jndi\s*:", re.IGNORECASE),
]
MAX_DOCS_TYPO_FILE_BYTES = 1_000_000  # 1 MB ceiling per file
MAX_DOCS_TYPO_FILES = 50              # ceiling on number of files modified


# ── Result container ───────────────────────────────────────────────────


@dataclass
class ExecResult:
    result: str = "failed"            # success|failed|noop|skipped|ci_failed_after_open
    reason: str = ""
    branch: str = ""
    pr_url: str = ""
    files_changed: int = 0
    cost_estimate: float = 0.0
    profile: str = ""
    item_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Sandbox checks ─────────────────────────────────────────────────────


def _path_under_forbidden_prefix(p: Path) -> bool:
    """True iff ``p`` resolves under any FORBIDDEN_PATH_PREFIXES entry."""
    try:
        resolved = str(p.resolve())
    except (OSError, RuntimeError):
        return True  # fail-closed
    for prefix in FORBIDDEN_PATH_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _validate_repo_path(repo_path: Path) -> Tuple[bool, str]:
    if not repo_path.exists():
        return False, "repo_not_found"
    if not repo_path.is_dir():
        return False, "repo_not_dir"
    if _path_under_forbidden_prefix(repo_path):
        return False, "repo_in_forbidden_prefix"
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return False, "not_a_git_repo"
    return True, ""


def _validate_branch_name(branch: str) -> Tuple[bool, str]:
    """Branch must start with ``auto/`` and not match a protected base."""
    if not branch.startswith("auto/"):
        return False, "branch_must_start_with_auto/"
    suffix = branch[len("auto/"):]
    head = suffix.split("/")[0].split("-")[0].lower()
    if head in FORBIDDEN_BASE_BRANCHES:
        return False, f"branch_collides_with_protected:{head}"
    return True, ""


# ── Branch + commit helpers ────────────────────────────────────────────


def _short_hash(seed: str, n: int = 6) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:n]


def make_branch_name(profile: str, item_id: str, *, seed: Optional[str] = None) -> str:
    """``auto/{profile}-{item_id}-{short_hash}``.

    The short hash is derived from ``seed`` (defaults to a process-
    unique string) so two crons running for the same item won't open
    PRs from the same branch name. Concurrency=1 makes that mostly
    moot but the hash gives an extra safety belt.
    """
    seed = seed or f"{profile}:{item_id}:{os.getpid()}"
    return f"auto/{profile}-{item_id}-{_short_hash(seed)}"


def _run_git(
    repo_path: Path,
    args: List[str],
    *,
    runner: Optional[Callable] = None,
    timeout: int = 60,
    check: bool = False,
) -> Tuple[int, str, str]:
    cmd = ["git", *args]
    if runner is not None:
        proc = runner(cmd, cwd=str(repo_path))
        return (
            getattr(proc, "returncode", 1),
            getattr(proc, "stdout", "") or "",
            getattr(proc, "stderr", "") or "",
        )
    try:
        p = subprocess.run(
            cmd, cwd=str(repo_path), capture_output=True, text=True,
            timeout=timeout, check=check,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _ensure_clean_worktree(repo_path: Path, runner=None) -> Tuple[bool, str]:
    rc, stdout, _ = _run_git(repo_path, ["status", "--porcelain"], runner=runner)
    if rc != 0:
        return False, "git_status_failed"
    if stdout.strip():
        return False, "worktree_dirty"
    return True, ""


def _create_branch(repo_path: Path, branch: str, runner=None) -> Tuple[bool, str]:
    ok, reason = _validate_branch_name(branch)
    if not ok:
        return False, reason
    rc, _, stderr = _run_git(repo_path, ["checkout", "-b", branch], runner=runner)
    if rc != 0:
        return False, f"checkout_failed: {stderr.strip()[:200]}"
    return True, ""


def _commit_all(
    repo_path: Path,
    message: str,
    runner=None,
) -> Tuple[bool, str, int]:
    """Stage everything, commit, return (ok, reason, files_changed)."""
    rc, stdout, _ = _run_git(repo_path, ["status", "--porcelain"], runner=runner)
    if rc != 0:
        return False, "git_status_failed", 0
    files_changed = sum(1 for ln in stdout.splitlines() if ln.strip())
    if files_changed == 0:
        return True, "no_changes", 0
    rc, _, stderr = _run_git(repo_path, ["add", "-A"], runner=runner)
    if rc != 0:
        return False, f"add_failed: {stderr.strip()[:200]}", 0
    rc, _, stderr = _run_git(
        repo_path, ["commit", "-m", message], runner=runner, timeout=120,
    )
    if rc != 0:
        return False, f"commit_failed: {stderr.strip()[:200]}", 0
    return True, "", files_changed


def _push_branch(
    repo_path: Path,
    branch: str,
    runner=None,
) -> Tuple[bool, str]:
    """Push ``branch`` to origin. NEVER --no-verify, NEVER --force.

    Defensive: re-validate the branch name immediately before push so a
    test that bypasses ``_create_branch`` can't sneak a push to main
    through this helper.
    """
    ok, reason = _validate_branch_name(branch)
    if not ok:
        return False, reason
    rc, _, stderr = _run_git(
        repo_path,
        ["push", "--set-upstream", "origin", branch],
        runner=runner,
        timeout=120,
    )
    if rc != 0:
        return False, f"push_failed: {stderr.strip()[:200]}"
    return True, ""


def _open_pr(
    repo_path: Path,
    *,
    branch: str,
    title: str,
    body: str,
    runner=None,
) -> Tuple[bool, str, str]:
    """Open a PR via ``gh pr create``. Returns (ok, pr_url, reason).

    Daemon never auto-merges; the PR is opened in the default state.
    """
    cmd = [
        "gh", "pr", "create",
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    if runner is not None:
        proc = runner(cmd, cwd=str(repo_path))
        rc = getattr(proc, "returncode", 1)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
    else:
        try:
            p = subprocess.run(
                cmd, cwd=str(repo_path), capture_output=True,
                text=True, timeout=60, check=False,
            )
            rc, stdout, stderr = p.returncode, p.stdout, p.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", f"gh_pr_create_failed: {exc}"
    if rc != 0:
        return False, "", f"gh_pr_create_failed: {stderr.strip()[:200]}"
    pr_url = (stdout or "").strip().splitlines()[-1] if stdout else ""
    return True, pr_url, ""


# ── Profile: format_fix ────────────────────────────────────────────────


def _detect_format_command(repo_path: Path) -> Optional[List[str]]:
    """Return a deterministic format command, or None.

    Order:
        1. ``package.json`` script ``format`` (npm run format)
        2. ``package.json`` script ``lint:fix``
        3. ``prettier --write .`` if prettier is on PATH AND a
           ``.prettierrc*`` config exists.
        4. Python: ``black .`` if a ``pyproject.toml`` has black config
           AND ``black`` is on PATH.

    Lock to the first hit. We don't auto-introduce a formatter to a
    repo that didn't already configure one — that would be intrusive.
    """
    pkg = repo_path / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = (data.get("scripts") or {}) if isinstance(data, dict) else {}
            if isinstance(scripts, dict):
                for key in ("format", "lint:fix"):
                    if key in scripts and isinstance(scripts[key], str) and scripts[key].strip():
                        return ["npm", "run", key]
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    # Prettier with config
    prettier_configs = [
        ".prettierrc", ".prettierrc.json", ".prettierrc.yaml",
        ".prettierrc.yml", ".prettierrc.js", "prettier.config.js",
    ]
    if shutil.which("prettier") and any((repo_path / c).exists() for c in prettier_configs):
        return ["prettier", "--write", "."]
    # Black
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists() and shutil.which("black"):
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.black]" in content:
                return ["black", "."]
        except OSError:
            pass
    return None


def execute_format_fix(
    *,
    repo_path: Path,
    item_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    runner: Optional[Callable] = None,
) -> ExecResult:
    """Run formatter; commit + return success only if files changed.

    The PR is NOT opened here — that's the caller's responsibility
    after the gate passes. This function returns either:
        - ``noop`` when no files changed
        - ``failed`` when something broke
        - ``success`` (pre-PR) when a commit landed on the new branch
          and the caller can run the gate + open PR.
    """
    out = ExecResult(profile="format_fix", item_id=item_id)
    ok, reason = _validate_repo_path(repo_path)
    if not ok:
        out.reason = reason
        return out
    ok, reason = _ensure_clean_worktree(repo_path, runner=runner)
    if not ok:
        # Precondition mismatch (worktree dirty), not a profile failure.
        # Mark skipped so the consecutive-failure breaker doesn't trip.
        out.result = "skipped"
        out.reason = reason
        return out

    cmd = _detect_format_command(repo_path)
    if cmd is None:
        # Repo has no formatter config — daemon can't run, but this is
        # a setup gap not an executor failure. Skip rather than fail.
        out.result = "skipped"
        out.reason = "no_formatter_detected"
        return out

    branch = make_branch_name("format_fix", item_id)
    out.branch = branch
    ok, reason = _create_branch(repo_path, branch, runner=runner)
    if not ok:
        out.reason = reason
        return out

    # Run the formatter.
    if runner is not None:
        proc = runner(cmd, cwd=str(repo_path))
        rc = getattr(proc, "returncode", 1)
        stderr = getattr(proc, "stderr", "") or ""
    else:
        try:
            p = subprocess.run(
                cmd, cwd=str(repo_path), capture_output=True, text=True,
                timeout=300, check=False,
            )
            rc, stderr = p.returncode, p.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            out.reason = f"formatter_failed: {exc}"
            return out
    if rc != 0:
        out.reason = f"formatter_returned_nonzero: {stderr.strip()[:200]}"
        return out

    ok, reason, files = _commit_all(
        repo_path, f"chore(led193): format_fix for {item_id}", runner=runner,
    )
    out.files_changed = files
    if not ok:
        out.reason = reason
        return out
    if files == 0:
        out.result = "noop"
        out.reason = "no_changes_after_format"
        return out

    out.result = "success"
    return out


# ── Profile: lockfile_refresh ──────────────────────────────────────────


def _detect_lockfile_command(repo_path: Path) -> Optional[Tuple[List[str], str]]:
    """Return (cmd, lockfile_basename) or None."""
    if (repo_path / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        return (["pnpm", "install", "--lockfile-only"], "pnpm-lock.yaml")
    if (repo_path / "yarn.lock").exists() and shutil.which("yarn"):
        # yarn classic / berry both refresh on `install --mode update-lockfile`
        return (["yarn", "install", "--mode", "update-lockfile"], "yarn.lock")
    if (repo_path / "package-lock.json").exists() and shutil.which("npm"):
        return (["npm", "install", "--package-lock-only"], "package-lock.json")
    if (repo_path / "poetry.lock").exists() and shutil.which("poetry"):
        return (["poetry", "lock", "--no-update"], "poetry.lock")
    if (repo_path / "Pipfile.lock").exists() and shutil.which("pipenv"):
        return (["pipenv", "lock"], "Pipfile.lock")
    return None


def execute_lockfile_refresh(
    *,
    repo_path: Path,
    item_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    runner: Optional[Callable] = None,
) -> ExecResult:
    out = ExecResult(profile="lockfile_refresh", item_id=item_id)
    ok, reason = _validate_repo_path(repo_path)
    if not ok:
        out.reason = reason
        return out
    ok, reason = _ensure_clean_worktree(repo_path, runner=runner)
    if not ok:
        out.result = "skipped"
        out.reason = reason
        return out

    detected = _detect_lockfile_command(repo_path)
    if detected is None:
        out.result = "skipped"
        out.reason = "no_lockfile_or_manager_detected"
        return out
    cmd, _lockfile = detected

    branch = make_branch_name("lockfile_refresh", item_id)
    out.branch = branch
    ok, reason = _create_branch(repo_path, branch, runner=runner)
    if not ok:
        out.reason = reason
        return out

    if runner is not None:
        proc = runner(cmd, cwd=str(repo_path))
        rc = getattr(proc, "returncode", 1)
        stderr = getattr(proc, "stderr", "") or ""
    else:
        try:
            p = subprocess.run(
                cmd, cwd=str(repo_path), capture_output=True, text=True,
                timeout=600, check=False,
            )
            rc, stderr = p.returncode, p.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            out.reason = f"lockfile_refresh_failed: {exc}"
            return out
    if rc != 0:
        out.reason = f"lockfile_manager_nonzero: {stderr.strip()[:200]}"
        return out

    ok, reason, files = _commit_all(
        repo_path, f"chore(led193): lockfile_refresh for {item_id}", runner=runner,
    )
    out.files_changed = files
    if not ok:
        out.reason = reason
        return out
    if files == 0:
        out.result = "noop"
        out.reason = "no_lockfile_change"
        return out

    out.result = "success"
    return out


# ── Profile: docs_typo ─────────────────────────────────────────────────


def _validate_docs_typo_metadata(metadata: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(metadata, dict):
        return False, "metadata_missing_or_invalid"
    find_string = metadata.get("find_string")
    replace_string = metadata.get("replace_string")
    file_glob = metadata.get("file_glob")
    if not isinstance(find_string, str) or len(find_string) < 3:
        return False, "find_string_too_short_or_missing"
    if not isinstance(replace_string, str):
        return False, "replace_string_missing"
    if not isinstance(file_glob, str) or not file_glob.strip():
        return False, "file_glob_missing"
    # Reject dangerous strings — both find AND replace get scanned
    # because someone could try to "fix" a comment that injects script.
    for pat in DANGEROUS_REPLACE_PATTERNS:
        if pat.search(find_string) or pat.search(replace_string):
            return False, "dangerous_pattern_in_strings"
    # Cap find/replace length so a megabyte payload can't sneak in.
    if len(find_string) > 500 or len(replace_string) > 500:
        return False, "find_or_replace_too_long"
    return True, ""


def _glob_files(repo_path: Path, glob: str) -> List[Path]:
    """Return files matching ``glob`` under ``repo_path``.

    ``glob`` is interpreted relative to the repo root. ``Path.glob``
    supports ``**`` recursion — perfect for callers writing things like
    ``docs/**/*.md``. We reject path-traversal attempts (``..``) and
    bound the result count.
    """
    if ".." in Path(glob).parts:
        return []
    matches: List[Path] = []
    try:
        # Path.glob handles ``**`` correctly (fnmatch doesn't).
        iterator = repo_path.glob(glob)
    except (ValueError, OSError):
        return []
    for path in iterator:
        if not path.is_file():
            continue
        # Skip anything inside .git/ — never modify VCS metadata.
        try:
            rel_parts = path.relative_to(repo_path).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] == ".git":
            continue
        matches.append(path)
        if len(matches) > MAX_DOCS_TYPO_FILES * 4:
            break  # don't walk forever
    return matches


def execute_docs_typo(
    *,
    repo_path: Path,
    item_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    runner: Optional[Callable] = None,
) -> ExecResult:
    out = ExecResult(profile="docs_typo", item_id=item_id)
    ok, reason = _validate_repo_path(repo_path)
    if not ok:
        out.reason = reason
        return out
    ok, reason = _ensure_clean_worktree(repo_path, runner=runner)
    if not ok:
        out.result = "skipped"
        out.reason = reason
        return out

    md = metadata or {}
    ok, reason = _validate_docs_typo_metadata(md)
    if not ok:
        out.reason = reason
        return out
    find_string = md["find_string"]
    replace_string = md["replace_string"]
    file_glob = md["file_glob"]

    candidates = _glob_files(repo_path, file_glob)
    if not candidates:
        out.result = "noop"
        out.reason = "no_files_matched_glob"
        return out

    branch = make_branch_name("docs_typo", item_id)
    out.branch = branch
    ok, reason = _create_branch(repo_path, branch, runner=runner)
    if not ok:
        out.reason = reason
        return out

    files_changed = 0
    for path in candidates:
        # File-size guard
        try:
            sz = path.stat().st_size
        except OSError:
            continue
        if sz > MAX_DOCS_TYPO_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if find_string not in text:
            continue
        new_text = text.replace(find_string, replace_string)
        if new_text == text:
            continue
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError:
            continue
        files_changed += 1
        if files_changed >= MAX_DOCS_TYPO_FILES:
            break

    out.files_changed = files_changed
    if files_changed == 0:
        out.result = "noop"
        out.reason = "find_string_not_present"
        return out

    ok, reason, committed = _commit_all(
        repo_path, f"docs(led193): typo fix for {item_id}", runner=runner,
    )
    if not ok:
        out.reason = reason
        return out
    if committed == 0:
        # Shouldn't happen (we just wrote files) but defend anyway
        out.result = "noop"
        out.reason = "commit_saw_no_changes"
        return out
    out.files_changed = committed
    out.result = "success"
    return out


# ── Dispatcher ─────────────────────────────────────────────────────────


PROFILE_DISPATCH = {
    "format_fix": execute_format_fix,
    "lockfile_refresh": execute_lockfile_refresh,
    "docs_typo": execute_docs_typo,
}


def execute_item(
    *,
    profile: str,
    item: Dict[str, Any],
    repo_path: Path,
    runner: Optional[Callable] = None,
) -> ExecResult:
    """Dispatch to the right profile executor.

    Returns an ExecResult with ``result`` in {success, failed, noop}.
    The caller (``scripts/led193_cron.py``) then decides whether to run
    the pre-push gate, push, and open the PR — based on the result.
    """
    fn = PROFILE_DISPATCH.get(profile)
    if fn is None:
        return ExecResult(
            result="failed",
            reason=f"unknown_profile:{profile}",
            profile=profile,
            item_id=item.get("id", ""),
        )
    item_id = item.get("id") or ""
    if not item_id:
        return ExecResult(
            result="failed",
            reason="item_missing_id",
            profile=profile,
        )
    metadata = item.get("metadata") or {}
    return fn(
        repo_path=repo_path,
        item_id=item_id,
        metadata=metadata,
        runner=runner,
    )


# ── Reject-helpers (used by tests + cron) ──────────────────────────────


def reject_no_verify(args: List[str]) -> bool:
    """Return True iff ``--no-verify`` is in the arg list."""
    return any(a == "--no-verify" for a in args or [])


def reject_force_push(args: List[str]) -> bool:
    return any(a in ("-f", "--force", "--force-with-lease") for a in args or [])
