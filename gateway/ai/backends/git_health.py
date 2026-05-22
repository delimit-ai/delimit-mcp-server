"""Git worktree sanity checks (LED-1411).

Single source of truth for "is this directory a healthy git worktree?"
Used by delimit_test_smoke, delimit_deploy_plan, and delimit_evidence_collect
as a precheck before they trust ambient checkout state.

Background — LED-1403 / LED-1401 incident (2026-05-14):
`/home/delimit/npm-delimit/.git` was configured `bare = true` but had source
files alongside, AND a stranded sibling worktree at `/tmp/delimit-mcp-main`
where `git status` showed every file as both `D` and `??` (deleted from
index, untracked on disk). `delimit_test_smoke` ran against this corrupt
state and reported `attest-mcp Q2 3-tier exit codes` failures that did NOT
exist on real main. I almost shipped a "fix" for a non-bug (LED-1403,
closed `not_reproducible` after a fresh clone proved tests passed).

This module exists so the same class of phantom failure can't recur.
Precheck must:
  - Add <100ms to caller startup (no network, no fetch)
  - Emit a single actionable remediation line on failure
  - Return a structured dict (callers may inline-handle or surface up)

Memory anchor: feedback_corrupted_worktree_phantom_failures.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict


def _run(cmd: list, cwd: str, timeout: float = 2.0) -> str:
    """Run a git command with a tight timeout. Returns stdout stripped,
    or empty string on any failure (intentional — caller decides what
    constitutes a failure based on the structured result, not exceptions)."""
    try:
        return subprocess.check_output(
            cmd,
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def check_worktree_sanity(repo_path: str) -> Dict[str, Any]:
    """Verify the directory at `repo_path` is a healthy git worktree.

    Checks (in order; cheapest first):
      1. Path exists and contains a `.git` directory (or file pointing to one)
      2. `git rev-parse --is-inside-work-tree` returns `true`
      3. `git rev-parse --is-bare-repository` returns `false`
      4. `git worktree list` includes the resolved CWD
      5. `git status --porcelain=v1` does NOT show every file as BOTH
         deleted-from-index AND untracked (the LED-1401 corruption signature)

    Returns a dict with:
      - ok: bool — overall health
      - reason: str — short failure code (`not_a_repo`, `bare_repo_with_files`,
        `stranded_worktree`, `corrupt_status`) when ok=False, else `healthy`
      - detail: str — actionable remediation message
      - path: str — the path that was checked

    Non-raising: errors return ok=False with a structured reason, so callers
    can decide whether to halt or warn.
    """
    p = Path(repo_path)
    if not p.exists() or not p.is_dir():
        return {
            "ok": False,
            "reason": "not_a_directory",
            "detail": f"{repo_path} is not a directory.",
            "path": repo_path,
        }

    git_meta = p / ".git"
    if not git_meta.exists():
        return {
            "ok": False,
            "reason": "not_a_repo",
            "detail": f"{repo_path} has no .git/ — not a git worktree.",
            "path": repo_path,
        }

    # Bare-repo check first (LED-1401 signature: bare=true + source files
    # alongside). Checked BEFORE is-inside-work-tree because a bare repo
    # answers "false" to that question — we want the more informative
    # bare-repo message to win when both conditions hold.
    is_bare = _run(["git", "rev-parse", "--is-bare-repository"], cwd=repo_path)
    if is_bare == "true":
        return {
            "ok": False,
            "reason": "bare_repo_with_files",
            "detail": (
                f"{repo_path}/.git/ has `core.bare = true` but the directory "
                f"holds source files. Tests against this state run stale "
                f"code. Re-clone fresh: `git clone <url> /tmp/<repo>-fresh "
                f"&& cd /tmp/<repo>-fresh`"
            ),
            "path": repo_path,
        }

    # Inside-work-tree check
    inside = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path)
    if inside != "true":
        return {
            "ok": False,
            "reason": "not_a_worktree",
            "detail": (
                f"{repo_path} is not inside a git work tree "
                f"(rev-parse --is-inside-work-tree returned {inside!r}). "
                f"Re-clone fresh: `git clone <url> /tmp/<repo>-fresh && cd /tmp/<repo>-fresh`"
            ),
            "path": repo_path,
        }

    # Worktree-list membership check (catches stranded sibling worktrees)
    worktrees = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    resolved = str(p.resolve())
    if worktrees and resolved not in worktrees:
        # The current directory isn't a registered worktree of its own
        # .git/ — likely a stale checkout that was wiped+repopulated outside
        # git's awareness. This is the LED-1401 stranded-sibling signature.
        return {
            "ok": False,
            "reason": "stranded_worktree",
            "detail": (
                f"{resolved} is not a registered worktree of its own .git/. "
                f"Run `git worktree list` to inspect; re-clone fresh if "
                f"orphaned: `git clone <url> /tmp/<repo>-fresh && cd /tmp/<repo>-fresh`"
            ),
            "path": repo_path,
            "worktree_list": worktrees,
        }

    # LED-1401 corrupt-status signature: every file appears as BOTH `D` and `??`
    # (deleted from index, untracked on disk). Sample the first 50 status lines
    # — if >=10 distinct paths show this pattern, it's pathological.
    status = _run(["git", "status", "--porcelain=v1"], cwd=repo_path, timeout=3.0)
    if status:
        lines = status.split("\n")[:200]
        deleted_paths = set()
        untracked_paths = set()
        for line in lines:
            if len(line) < 4:
                continue
            xy = line[:2]
            path = line[3:].lstrip()
            if "D" in xy:
                deleted_paths.add(path)
            if xy == "??":
                untracked_paths.add(path)
        overlap = deleted_paths & untracked_paths
        if len(overlap) >= 10:
            return {
                "ok": False,
                "reason": "corrupt_status",
                "detail": (
                    f"{repo_path} shows >={len(overlap)} files as both deleted-from-index "
                    f"AND untracked-on-disk — the worktree was wiped and repopulated "
                    f"outside git's awareness (LED-1401 signature). Re-clone fresh: "
                    f"`git clone <url> /tmp/<repo>-fresh && cd /tmp/<repo>-fresh`"
                ),
                "path": repo_path,
                "overlap_count": len(overlap),
            }

    return {
        "ok": True,
        "reason": "healthy",
        "detail": "git worktree is healthy",
        "path": repo_path,
    }
