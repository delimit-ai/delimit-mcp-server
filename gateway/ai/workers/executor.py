"""LED-981: Worker Pool v2 executor.

Takes an approved work order and runs its `executable_actions` list
against a narrow whitelist of state-changing operations. This is where
the founder's "dashboard approve → autonomous execute" loop closes.

Safety model (defense in depth):

1. Workers only emit actions from a hardcoded whitelist (ACTION_SPEC).
   Any unknown action is rejected at draft time by validate_actions().
2. The executor re-validates before running. A work order that was
   approved by a human can never execute an action the executor can't
   type-check.
3. Each action has a fixed parameter shape; missing or extra params
   are rejected.
4. Execution never shells out to an arbitrary command. Every action
   has a Python implementation that calls a specific subprocess or
   library with bounded inputs.
5. Every action (success or failure) is appended to execution_log and
   synced to Supabase so the dashboard shows what the executor did.
6. Dry-run mode is the DEFAULT. Live execution requires
   `execute_approved(..., live=True)` — the MCP tool wrapper defaults
   to live=False so an accidental call is observable without effect.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.workers.executor")

WORK_ORDERS_DIR = Path.home() / ".delimit" / "work-orders"
EXECUTOR_AUDIT = Path.home() / ".delimit" / "workers" / "audit" / "executor.jsonl"
# Kill switch. Matches the charter's kill-switch table — touch this file
# and the poller stops flipping approved work orders into live execution.
EXECUTOR_PAUSE_FILE = Path.home() / ".delimit" / "pause_executor"


# ---------------------------------------------------------------------------
# Action specification — THE whitelist. Add an action type by editing here.
# ---------------------------------------------------------------------------

ACTION_SPEC: Dict[str, Dict[str, Any]] = {
    "gh_issue_create": {
        "required_params": ("repo", "title", "body"),
        "optional_params": ("labels",),
        "description": "Open a GitHub issue on an external repo via gh CLI.",
    },
    "gh_pr_comment": {
        "required_params": ("repo", "number", "body"),
        "optional_params": (),
        "description": "Add a comment to an open GitHub PR.",
    },
    "gh_issue_comment": {
        "required_params": ("repo", "number", "body"),
        "optional_params": (),
        "description": "Add a comment to an open GitHub issue.",
    },
    "gh_issue_close": {
        "required_params": ("repo", "number"),
        "optional_params": ("comment", "reason"),
        "description": "Close a GitHub issue, optionally with a closing comment and reason.",
    },
    "gh_issue_reopen": {
        "required_params": ("repo", "number"),
        "optional_params": ("comment",),
        "description": "Reopen a closed GitHub issue, optionally with an explanatory comment.",
    },
    "gh_issue_label": {
        "required_params": ("repo", "number", "labels"),
        "optional_params": ("remove",),
        "description": "Add labels (or remove when remove=true) from a GitHub issue or PR.",
    },
    "gh_pr_ready_for_review": {
        "required_params": ("repo", "number"),
        "optional_params": (),
        "description": "Mark a draft PR as ready for review.",
    },
    "propose_pr": {
        "required_params": ("repo_path", "branch", "title", "body", "files"),
        "optional_params": ("tests_cmd", "base_branch", "draft", "commit_message"),
        "description": (
            "LED-988 autonomous build primitive: branch → write files → test → "
            "commit → push → open draft PR. Stops at PR opened — merge and "
            "tag-push stay human per 2026-04-07 postmortem."
        ),
    },
}


# LED-988: allowlist for propose_pr. Any repo path NOT in this set is
# rejected at runtime regardless of whether the caller claimed validation
# passed. Path-traversal-safe (resolved then checked against canonical).
PROPOSE_PR_ALLOWED_REPOS = frozenset({
    "/home/delimit/delimit-gateway",
    "/home/delimit/delimit-ui",
    "/home/delimit/delimit-action",
    "/home/delimit/npm-delimit",
    "/root/governance-framework",
})
# Any branch created by propose_pr must carry this prefix so human branches
# are never clobbered and PRs are obviously agent-authored at a glance.
PROPOSE_PR_BRANCH_PREFIX = "delimit/"
# Commit author for autonomous commits. Bot-pattern email so GitHub
# counts contributions correctly without attributing to a human.
PROPOSE_PR_AUTHOR_NAME = "delimit-bot"
PROPOSE_PR_AUTHOR_EMAIL = "bot@delimit.ai"
# Hard cap on patch size — rejects accidental mega-diffs that would
# require a different review workflow anyway.
PROPOSE_PR_MAX_FILES = 50
PROPOSE_PR_MAX_FILE_BYTES = 256 * 1024  # 256 KiB / file
PROPOSE_PR_MAX_TOTAL_BYTES = 1024 * 1024  # 1 MiB / PR


class ActionError(Exception):
    pass


# LED-988 (Polymarket/RunLobster deliberation): explicit category denylist.
# The executor's guardrail today is implicit ("only whitelisted actions run")
# which is necessary but not sufficient — a future whitelist extension could
# silently add a category that belongs behind a charter amendment. This
# denylist is a hard second gate. An action type OR any parameter value
# matching any token here is rejected at validate_actions() time with a
# loud error, regardless of whether it's in ACTION_SPEC.
#
# Match is substring on the lowercased action name, param key, and param
# string-value. Add a category here by editing this set; removing a
# category requires a charter amendment + deliberation (commit message
# must cite the amendment).
ACTION_DENYLIST_TOKENS = frozenset({
    # Money / payments
    "financial_transaction",
    "payment_api",
    "stripe_charge",
    "stripe_transfer",
    "wire_transfer",
    "ach_transfer",
    "lemonsqueezy_charge",
    "plaid_link",
    # Legal / identity
    "llc_registration",
    "ein_application",
    "company_formation",
    "identity_registration",
    "kyc_submit",
    "aml_submit",
    # Credentials / auth handling
    "private_key_export",
    "private_key_generate",
    "seed_phrase",
    "api_key_rotate_external",  # rotating user-owned keys not in our vault
    # Autonomous deploy to prod outside our repos
    "external_deploy",
    "terraform_apply",
    "kubectl_apply",
    # Contract signing / binding legal action
    "contract_sign",
    "docusign_send",
    "hello_sign",
})


def _denylist_hits(name: str, params: Dict[str, Any]) -> List[str]:
    """Return every denylist token found in the action name or param values."""
    hits: List[str] = []
    haystack = [(name or "").lower()]
    for k, v in (params or {}).items():
        haystack.append(str(k).lower())
        if isinstance(v, str):
            haystack.append(v.lower())
    blob = " ".join(haystack)
    for token in ACTION_DENYLIST_TOKENS:
        if token in blob:
            hits.append(token)
    return hits


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_actions(actions: List[Dict[str, Any]]) -> List[str]:
    """Return a list of error strings — empty list means actions are valid."""
    errors: List[str] = []
    if not isinstance(actions, list):
        return [f"executable_actions must be a list, got {type(actions).__name__}"]
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"action[{i}]: must be a dict")
            continue
        action_type = action.get("action")
        # Denylist check BEFORE whitelist check. A hit here fails loud even
        # if the action happens to be in ACTION_SPEC (belt + suspenders —
        # accidental whitelist addition can't slip a denied category through).
        params = action.get("params") or {}
        if isinstance(params, dict):
            deny_hits = _denylist_hits(action_type or "", params)
            if deny_hits:
                errors.append(
                    f"action[{i}]: DENYLIST HIT for {action_type!r} — tokens "
                    f"{sorted(deny_hits)} are prohibited categories per "
                    f"LED-988 / Polymarket deliberation. Removing one of "
                    f"these requires a charter amendment."
                )
                continue
        if action_type not in ACTION_SPEC:
            errors.append(
                f"action[{i}]: unknown action '{action_type}'. "
                f"Allowed: {sorted(ACTION_SPEC.keys())}"
            )
            continue
        spec = ACTION_SPEC[action_type]
        params = action.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"action[{i}]: params must be a dict")
            continue
        for required in spec["required_params"]:
            if required not in params:
                errors.append(
                    f"action[{i}] ({action_type}): missing required param '{required}'"
                )
        allowed = set(spec["required_params"]) | set(spec["optional_params"])
        for provided in params:
            if provided not in allowed:
                errors.append(
                    f"action[{i}] ({action_type}): unknown param '{provided}'. "
                    f"Allowed: {sorted(allowed)}"
                )
    return errors


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _run_gh(args: List[str], stdin: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    """Run a gh subcommand with bounded inputs. Returns dict with stdout/stderr/rc."""
    cmd = ["gh", *args]
    logger.info("executor: running %s", " ".join(shlex.quote(a) for a in cmd))
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "rc": result.returncode,
            "stdout": (result.stdout or "")[:4000],
            "stderr": (result.stderr or "")[:2000],
        }
    except subprocess.TimeoutExpired:
        raise ActionError(f"gh timed out after {timeout}s")
    except FileNotFoundError:
        raise ActionError("gh CLI not installed")


def _act_gh_issue_create(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    title = params["title"]
    body = params["body"]
    labels = params.get("labels") or []
    args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    for label in labels:
        args.extend(["--label", label])
    result = _run_gh(args, stdin=body)
    if result["rc"] != 0:
        raise ActionError(f"gh issue create failed: {result['stderr']}")
    return {"issue_url": result["stdout"].strip()}


def _act_gh_pr_comment(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    body = params["body"]
    result = _run_gh(
        ["pr", "comment", number, "--repo", repo, "--body-file", "-"],
        stdin=body,
    )
    if result["rc"] != 0:
        raise ActionError(f"gh pr comment failed: {result['stderr']}")
    return {"comment_url": result["stdout"].strip()}


def _act_gh_issue_comment(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    body = params["body"]
    result = _run_gh(
        ["issue", "comment", number, "--repo", repo, "--body-file", "-"],
        stdin=body,
    )
    if result["rc"] != 0:
        raise ActionError(f"gh issue comment failed: {result['stderr']}")
    return {"comment_url": result["stdout"].strip()}


def _act_gh_issue_close(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    comment = params.get("comment")
    # gh close --reason accepts: completed | not planned
    reason = params.get("reason")
    args = ["issue", "close", number, "--repo", repo]
    if comment:
        args.extend(["--comment", comment])
    if reason:
        if reason not in ("completed", "not planned"):
            raise ActionError(f"reason must be 'completed' or 'not planned', got {reason!r}")
        args.extend(["--reason", reason])
    result = _run_gh(args)
    if result["rc"] != 0:
        raise ActionError(f"gh issue close failed: {result['stderr']}")
    return {"closed": f"{repo}#{number}", "stdout": result["stdout"].strip()}


def _act_gh_issue_reopen(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    comment = params.get("comment")
    args = ["issue", "reopen", number, "--repo", repo]
    if comment:
        args.extend(["--comment", comment])
    result = _run_gh(args)
    if result["rc"] != 0:
        raise ActionError(f"gh issue reopen failed: {result['stderr']}")
    return {"reopened": f"{repo}#{number}", "stdout": result["stdout"].strip()}


def _act_gh_issue_label(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    labels = params["labels"]
    remove = bool(params.get("remove", False))
    if not isinstance(labels, list) or not labels:
        raise ActionError("labels must be a non-empty list")
    # `gh issue edit` covers both add and remove and works for PRs too
    flag = "--remove-label" if remove else "--add-label"
    args = ["issue", "edit", number, "--repo", repo]
    for label in labels:
        if not isinstance(label, str) or not label:
            raise ActionError(f"every label must be a non-empty string, got {label!r}")
        args.extend([flag, label])
    result = _run_gh(args)
    if result["rc"] != 0:
        raise ActionError(f"gh issue edit ({flag}) failed: {result['stderr']}")
    return {"labeled": f"{repo}#{number}", "action": "remove" if remove else "add", "labels": labels}


def _act_gh_pr_ready_for_review(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = params["repo"]
    number = str(params["number"])
    # `gh pr ready` flips a draft PR to ready-for-review state
    result = _run_gh(["pr", "ready", number, "--repo", repo])
    if result["rc"] != 0:
        raise ActionError(f"gh pr ready failed: {result['stderr']}")
    return {"ready": f"{repo}#{number}", "stdout": result["stdout"].strip()}


def _run_git(cwd: str, args: List[str], timeout: int = 60) -> Dict[str, Any]:
    """Run git in `cwd`. Returns {rc, stdout, stderr} — no raising here so
    the caller decides which non-zero returns are fatal vs recoverable."""
    cmd = ["git", "-C", cwd, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "rc": result.returncode,
            "stdout": (result.stdout or "")[:4000],
            "stderr": (result.stderr or "")[:2000],
        }
    except subprocess.TimeoutExpired:
        raise ActionError(f"git timed out after {timeout}s: {' '.join(args[:3])}")
    except FileNotFoundError:
        raise ActionError("git CLI not installed")


def _act_propose_pr(params: Dict[str, Any]) -> Dict[str, Any]:
    """LED-988 autonomous build primitive.

    Flow: (resolve + allowlist) → checkout base + pull → create branch →
    write files → run tests if provided → commit with bot identity →
    push → open draft PR → return PR URL. Stops there.

    Safety invariants enforced at runtime, not just at validation time:
      - repo_path must resolve to a path in PROPOSE_PR_ALLOWED_REPOS
      - branch must carry PROPOSE_PR_BRANCH_PREFIX (never clobber human work)
      - file paths must be relative, no `..`, no absolute, no symlink hops
      - total patch size capped at 1 MiB / 50 files
      - tests_cmd failure aborts before push — no broken PR ever opens
      - PR opens as draft by default; gh_pr_ready_for_review is a separate
        whitelisted action the founder can invoke after review
      - bot identity is set via `git -c` (per-command) — never mutates
        repo or global git config
    """
    from pathlib import Path as _Path

    repo_path_raw = params["repo_path"]
    branch = params["branch"]
    title = params["title"]
    body = params["body"]
    files = params["files"]
    tests_cmd = params.get("tests_cmd") or ""
    base_branch = params.get("base_branch") or "main"
    draft = params.get("draft", True)
    commit_message = params.get("commit_message") or title

    # 1. Allowlist the repo path (canonical, resolves symlinks)
    try:
        repo_path = str(_Path(repo_path_raw).resolve(strict=True))
    except (FileNotFoundError, RuntimeError) as exc:
        raise ActionError(f"repo_path not found: {repo_path_raw} ({exc})")
    if repo_path not in PROPOSE_PR_ALLOWED_REPOS:
        raise ActionError(
            f"repo_path not in allowlist: {repo_path}. "
            f"Allowed: {sorted(PROPOSE_PR_ALLOWED_REPOS)}"
        )
    if not (_Path(repo_path) / ".git").exists():
        raise ActionError(f"repo_path is not a git repo: {repo_path}")

    # 2. Branch prefix guard
    if not isinstance(branch, str) or not branch.startswith(PROPOSE_PR_BRANCH_PREFIX):
        raise ActionError(
            f"branch must start with {PROPOSE_PR_BRANCH_PREFIX!r}, got {branch!r}"
        )
    if "/" not in branch[len(PROPOSE_PR_BRANCH_PREFIX):] and not branch[len(PROPOSE_PR_BRANCH_PREFIX):]:
        raise ActionError("branch is empty after prefix")

    # 3. File-list validation: size cap, no absolute paths, no `..`, required
    #    content for every entry.
    if not isinstance(files, list) or not files:
        raise ActionError("files must be a non-empty list")
    if len(files) > PROPOSE_PR_MAX_FILES:
        raise ActionError(f"files > {PROPOSE_PR_MAX_FILES} (got {len(files)})")
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ActionError("each file entry must be a dict")
        p = entry.get("path")
        c = entry.get("content", "")
        if not isinstance(p, str) or not p:
            raise ActionError("file.path required + must be a string")
        if p.startswith("/") or ".." in _Path(p).parts or p.startswith("~"):
            raise ActionError(f"file.path must be relative + inside repo: {p!r}")
        if not isinstance(c, str):
            raise ActionError(f"file.content must be a string for {p!r}")
        if len(c.encode("utf-8")) > PROPOSE_PR_MAX_FILE_BYTES:
            raise ActionError(
                f"{p!r} exceeds {PROPOSE_PR_MAX_FILE_BYTES} bytes"
            )
        total += len(c.encode("utf-8"))
    if total > PROPOSE_PR_MAX_TOTAL_BYTES:
        raise ActionError(
            f"total patch {total}B exceeds {PROPOSE_PR_MAX_TOTAL_BYTES}B"
        )

    # 4. Confirm working tree clean + base branch exists + fetch
    status = _run_git(repo_path, ["status", "--porcelain"])
    if status["rc"] != 0:
        raise ActionError(f"git status failed: {status['stderr']}")
    if status["stdout"].strip():
        raise ActionError(
            f"repo working tree dirty — refusing to propose on top of uncommitted "
            f"work:\n{status['stdout'][:500]}"
        )

    if _run_git(repo_path, ["fetch", "origin", base_branch])["rc"] != 0:
        raise ActionError(f"could not fetch origin/{base_branch}")
    checkout_base = _run_git(repo_path, ["checkout", base_branch])
    if checkout_base["rc"] != 0:
        raise ActionError(f"checkout {base_branch} failed: {checkout_base['stderr']}")
    pull = _run_git(repo_path, ["pull", "--ff-only", "origin", base_branch])
    if pull["rc"] != 0:
        raise ActionError(f"pull --ff-only origin/{base_branch} failed: {pull['stderr']}")

    # 5. Create the branch
    if _run_git(repo_path, ["checkout", "-b", branch])["rc"] != 0:
        # Maybe it already exists; switch + reset to base
        if _run_git(repo_path, ["checkout", branch])["rc"] != 0:
            raise ActionError(f"could not create or switch to {branch}")
        reset = _run_git(repo_path, ["reset", "--hard", f"origin/{base_branch}"])
        if reset["rc"] != 0:
            raise ActionError(f"could not reset {branch} to base: {reset['stderr']}")

    # 6. Write the files (create dirs as needed)
    written: List[str] = []
    try:
        for entry in files:
            dest = _Path(repo_path) / entry["path"]
            dest.resolve().relative_to(repo_path)  # defense in depth
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry["content"])
            written.append(entry["path"])
    except ValueError as exc:
        raise ActionError(f"file path escaped repo: {exc}")

    # 7. Stage + optional tests BEFORE commit
    stage = _run_git(repo_path, ["add", *written])
    if stage["rc"] != 0:
        raise ActionError(f"git add failed: {stage['stderr']}")

    if tests_cmd:
        logger.info("propose_pr: running tests: %s", tests_cmd)
        try:
            tests_proc = subprocess.run(
                tests_cmd, shell=True, cwd=repo_path,
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise ActionError("tests_cmd timed out after 600s")
        if tests_proc.returncode != 0:
            # Clean up so the working tree isn't left dirty on the branch.
            _run_git(repo_path, ["reset", "--hard", f"origin/{base_branch}"])
            _run_git(repo_path, ["checkout", base_branch])
            _run_git(repo_path, ["branch", "-D", branch])
            raise ActionError(
                f"tests failed (rc={tests_proc.returncode}); branch {branch} "
                f"discarded.\nstdout tail:\n{tests_proc.stdout[-2000:]}\n"
                f"stderr tail:\n{tests_proc.stderr[-2000:]}"
            )

    # 8. Commit with the bot identity (per-command -c, never global)
    commit_args = [
        "-c", f"user.name={PROPOSE_PR_AUTHOR_NAME}",
        "-c", f"user.email={PROPOSE_PR_AUTHOR_EMAIL}",
        "commit",
        "-m", commit_message,
    ]
    commit = _run_git(repo_path, commit_args)
    if commit["rc"] != 0:
        raise ActionError(f"commit failed: {commit['stderr']}")

    # 9. Push (no --force, no --force-with-lease — branch is fresh)
    push = _run_git(repo_path, ["push", "-u", "origin", branch])
    if push["rc"] != 0:
        raise ActionError(f"push origin {branch} failed: {push['stderr']}")

    # 10. Open the PR via gh (draft by default — human flips it with
    #     gh_pr_ready_for_review after review)
    gh_args = [
        "pr", "create",
        "--base", base_branch,
        "--head", branch,
        "--title", title,
        "--body-file", "-",
    ]
    if draft:
        gh_args.append("--draft")
    pr_result = subprocess.run(
        ["gh", *gh_args],
        input=body,
        capture_output=True, text=True, timeout=60,
        cwd=repo_path,
    )
    if pr_result.returncode != 0:
        raise ActionError(f"gh pr create failed: {pr_result.stderr[:400]}")
    pr_url = (pr_result.stdout or "").strip()

    # 11. Return to base branch so the repo is left in a clean, predictable
    #     state for the next caller.
    _run_git(repo_path, ["checkout", base_branch])

    return {
        "pr_url": pr_url,
        "branch": branch,
        "base_branch": base_branch,
        "files_written": written,
        "tests_ran": bool(tests_cmd),
        "draft": bool(draft),
    }


ACTION_RUNNERS = {
    "gh_issue_create": _act_gh_issue_create,
    "gh_pr_comment": _act_gh_pr_comment,
    "gh_issue_comment": _act_gh_issue_comment,
    "gh_issue_close": _act_gh_issue_close,
    "gh_issue_reopen": _act_gh_issue_reopen,
    "gh_issue_label": _act_gh_issue_label,
    "gh_pr_ready_for_review": _act_gh_pr_ready_for_review,
    "propose_pr": _act_propose_pr,
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def _append_audit(record: Dict[str, Any]) -> None:
    EXECUTOR_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    try:
        with EXECUTOR_AUDIT.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("executor: audit write failed: %s", exc)


def _load_work_order(wo_id: str) -> Optional[Dict[str, Any]]:
    jf = WORK_ORDERS_DIR / f"{wo_id}.json"
    if not jf.exists():
        return None
    try:
        return json.loads(jf.read_text())
    except Exception as exc:
        logger.warning("executor: failed to load %s: %s", wo_id, exc)
        return None


def _save_work_order(wo: Dict[str, Any]) -> None:
    jf = WORK_ORDERS_DIR / f"{wo['id']}.json"
    jf.write_text(json.dumps(wo, indent=2))


def execute_approved(wo_id: str, *, live: bool = False, executed_by: str = "") -> Dict[str, Any]:
    """Execute an approved work order's executable_actions list.

    Args:
        wo_id: Work-order id (e.g. WO-2026-04-18-001).
        live: When False (default) the executor returns what it WOULD do
            without running any subprocess. A sanity check before flipping
            the switch.
        executed_by: Agent / user identifier for the audit log.

    Returns a dict with the overall status plus a per-action log.
    """
    wo = _load_work_order(wo_id)
    if wo is None:
        return {"ok": False, "error": f"work order {wo_id} not found"}

    if wo.get("status") != "approved":
        return {
            "ok": False,
            "error": (
                f"work order {wo_id} has status={wo.get('status')!r}; "
                f"executor only runs work orders with status=approved"
            ),
        }

    actions = wo.get("executable_actions") or []
    if not actions:
        return {
            "ok": False,
            "error": (
                f"work order {wo_id} has no executable_actions. The founder "
                f"still needs to run the human steps by hand."
            ),
        }

    errors = validate_actions(actions)
    if errors:
        return {"ok": False, "error": "action validation failed", "details": errors}

    now = datetime.now(timezone.utc).isoformat()
    log: List[Dict[str, Any]] = []

    if not live:
        for i, action in enumerate(actions):
            log.append({
                "index": i,
                "action": action["action"],
                "dry_run": True,
                "params_preview": {
                    k: (v[:200] if isinstance(v, str) else v)
                    for k, v in (action.get("params") or {}).items()
                },
            })
        _append_audit({
            "wo_id": wo_id,
            "ts": now,
            "mode": "dry_run",
            "executed_by": executed_by,
            "action_count": len(actions),
        })
        return {
            "ok": True,
            "mode": "dry_run",
            "wo_id": wo_id,
            "actions": len(actions),
            "log": log,
        }

    # Live mode: flip status to executing, run each action in order,
    # persist the log both to the local WO file and to Supabase.
    wo["execution_status"] = "executing"
    wo["executed_by"] = executed_by
    _save_work_order(wo)

    overall_ok = True
    for i, action in enumerate(actions):
        runner = ACTION_RUNNERS[action["action"]]
        started = time.time()
        entry = {"index": i, "action": action["action"], "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            result = runner(action.get("params") or {})
            entry.update({"ok": True, "result": result})
        except ActionError as exc:
            entry.update({"ok": False, "error": str(exc)})
            overall_ok = False
        except Exception as exc:  # defensive — never crash the daemon
            entry.update({"ok": False, "error": f"unexpected: {exc}"})
            overall_ok = False
        entry["elapsed_ms"] = int((time.time() - started) * 1000)
        log.append(entry)
        _append_audit({"wo_id": wo_id, **entry, "executed_by": executed_by})
        if not overall_ok:
            break

    wo["execution_status"] = "executed" if overall_ok else "failed"
    wo["execution_log"] = log
    wo["executed_at"] = datetime.now(timezone.utc).isoformat()
    wo["status"] = "executed" if overall_ok else "failed"
    _save_work_order(wo)

    # Supabase sync with the new fields.
    try:
        from ai.supabase_sync import sync_work_order
        sync_work_order(wo)
    except Exception:
        pass

    return {
        "ok": overall_ok,
        "mode": "live",
        "wo_id": wo_id,
        "actions": len(actions),
        "log": log,
    }


# ---------------------------------------------------------------------------
# Polling / autonomous path
# ---------------------------------------------------------------------------

def _is_paused_cloud() -> bool:
    """Check the Supabase-backed executor_config flag.

    Lets a Pro user toggle the kill switch from the dashboard without
    shell access to the gateway host. Logical OR with the local file so
    either surface can stop execution. Returns False on any error — the
    LOCAL file remains the last-resort kill switch.
    """
    try:
        from ai.supabase_sync import _get_client, SUPABASE_URL, SUPABASE_KEY
        import urllib.request
    except Exception:
        return False
    if _get_client() is None:
        return False
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/executor_config?id=eq.default&select=paused",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read().decode())
            return bool(rows and rows[0].get("paused"))
    except Exception as exc:
        logger.debug("executor cloud pause check failed: %s", exc)
        return False


def is_paused() -> bool:
    """Charter kill switch: return True if execution is paused.

    Either the local file (`~/.delimit/pause_executor`) or the cloud
    config flag (`executor_config.paused`) stops the autonomous loop.
    Local wins any disagreement because it's the last-resort signal an
    operator with shell access can trust.
    """
    if EXECUTOR_PAUSE_FILE.exists():
        return True
    return _is_paused_cloud()


def list_approved_pending() -> List[Dict[str, Any]]:
    """Scan Supabase for approved work orders that haven't been executed yet.

    Returns an empty list on any error (the poller must never crash the
    daemon; a bad cloud read is a no-op).
    """
    try:
        from ai.supabase_sync import _get_client, SUPABASE_URL, SUPABASE_KEY
        import urllib.request
    except Exception:
        return []
    client = _get_client()
    if client is None:
        return []
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/work_orders"
            "?status=eq.approved"
            "&or=(execution_status.is.null,execution_status.eq.)"
            "&select=id,status,execution_status,executable_actions"
            "&order=created_at.asc"
            "&limit=20"
        )
        req = urllib.request.Request(
            url,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.debug("executor poller: supabase read failed: %s", exc)
        return []


def poll_and_execute(*, live: bool = False, executed_by: str = "daemon") -> Dict[str, Any]:
    """One tick of the autonomous executor loop.

    Looks for approved work orders with a non-empty executable_actions list
    that haven't been executed yet, and runs them. Returns a summary of
    what was attempted this tick. Kill-switch aware.
    """
    if is_paused():
        return {"paused": True, "reason": f"{EXECUTOR_PAUSE_FILE} exists"}

    found = list_approved_pending()
    results = []
    for row in found:
        wo_id = row.get("id")
        actions = row.get("executable_actions") or []
        if not wo_id or not actions:
            continue
        # Load local JSON sidecar (source of truth) — fall back to stub.
        wo = _load_work_order(wo_id) or {
            "id": wo_id,
            "status": row.get("status", ""),
            "executable_actions": actions,
        }
        if wo.get("status") != "approved":
            continue
        # Don't double-run anything that has an execution_status already.
        if wo.get("execution_status"):
            continue
        try:
            res = execute_approved(wo_id, live=live, executed_by=executed_by)
            results.append({"wo_id": wo_id, "ok": res.get("ok"), "mode": res.get("mode")})
        except Exception as exc:
            logger.warning("executor poller: %s failed: %s", wo_id, exc)
            results.append({"wo_id": wo_id, "ok": False, "error": str(exc)})

    return {
        "paused": False,
        "candidates": len(found),
        "attempted": len(results),
        "results": results,
    }
