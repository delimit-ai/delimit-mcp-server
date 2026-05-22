"""Inbox executor — LED-1134 Phase 2.

Closes the email→action loop: consumes the inbox-drafts registry written
by Phase 1 and dispatches autonomous actions when founder Ship-it
replies have transitioned drafts from pending → approved.

Constitutional reference: docs/inbox_executor_v1.md is the source of
truth for the wire contract, state machine, allowlist, and non-delegable
refusal list. Authorized via owner attestation 2026-04-26T02:49Z
(scope=authority_class_expansion, evidence_ref=LED-1134).

DESIGN INTENT (per the strategic + operational deliberations):

1. Separate process from inbox_daemon — daemon parses untrusted email
   (large attack surface); executor performs privileged actions (small
   attack surface). 3-1 panel vote against in-process consolidation.

2. Re-verify HMAC + TTL at execute time, not just at insert time.
   A draft sitting in the DB for 23h59m must NOT execute when it's
   24h+1m stale by the time we get to it.

3. Atomic transition approved → executing BEFORE the side effect.
   SQLite UPDATE with rowcount=1 wins; rowcount=0 means another
   instance already took it. At-most-once.

4. Crash mid-execute leaves the row at status=executing for human
   reconciliation. NO auto-retry — that turns at-most-once into
   at-least-once.

5. Non-delegable refusal list (per CLAUDE.md "Non-Delegable Decisions"):
   force_push_shared, ruleset_disable, account_switch, cross_account_ops,
   irreversible_capital_commit, constitutional_rewrite,
   authority_class_expansion, venture_kill, permission_escalation,
   public_truth_claim. ANY of these refuse, log, email founder for
   fresh attestation through different channel.

6. Thermal cutout — pause if more than N actions in T seconds. v1
   default: 10 actions / 15 minutes (Haiku's original 3-in-5min would
   trip on legitimate batch sweeps).

7. Allowlist of dispatch handlers — only github_comment is wired in
   PR-A; others land progressively.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai.inbox_drafts import (
    DraftRow,
    DraftStatus,
    list_drafts,
    record_attempt,
    transition,
    verify_draft,
)

logger = logging.getLogger("delimit.inbox_executor")

# State path for the daemon-thread control. Mirrors the inbox_daemon
# convention so operators can find both files in the same place.
STATE_PATH = Path.home() / ".delimit" / "inbox_executor_state.json"
DEFAULT_POLL_INTERVAL_SECONDS = 30


# ── Constitutional refusal list ──────────────────────────────────────


# Mirrors ai.governance.NON_DELEGABLE_OPERATION_CLASSES. We hard-code the
# set here so the executor can refuse without an import dependency that
# could theoretically be tampered. Both lists must stay in sync; the spec
# doc (docs/inbox_executor_v1.md) is the single source of truth.
NON_DELEGABLE_REFUSAL_LIST = frozenset({
    "force_push_shared",
    "ruleset_disable",
    "account_switch",
    "cross_account_ops",
    "irreversible_capital_commit",
    "constitutional_rewrite",
    "authority_class_expansion",
    "venture_kill",
    "permission_escalation",
    "public_truth_claim",
})


# ── Thermal cutout ────────────────────────────────────────────────────


@dataclass
class ThermalState:
    """Tracks recent action timestamps to detect bursts.

    Default: 10 actions / 15 minutes. Above the threshold the executor
    self-pauses for a cooldown. Per the deliberation: 3-in-5min would
    trip on legitimate batch sweeps (founder clearing 5–8 morning
    approvals at once); 10-in-15min is a real burst.
    """

    threshold_count: int = 10
    threshold_seconds: int = 15 * 60
    cooldown_seconds: int = 5 * 60
    recent_action_times: List[int] = field(default_factory=list)
    paused_until: int = 0

    def record(self, now: Optional[int] = None) -> None:
        if now is None:
            now = int(time.time())
        self.recent_action_times.append(now)
        # Drop entries older than the window.
        cutoff = now - self.threshold_seconds
        self.recent_action_times = [t for t in self.recent_action_times if t >= cutoff]
        if len(self.recent_action_times) > self.threshold_count:
            self.paused_until = now + self.cooldown_seconds
            logger.warning(
                "thermal cutout tripped: %d actions in last %ds; pausing %ds",
                len(self.recent_action_times),
                self.threshold_seconds,
                self.cooldown_seconds,
            )

    def is_paused(self, now: Optional[int] = None) -> bool:
        if now is None:
            now = int(time.time())
        return now < self.paused_until


# ── Dispatch handlers ────────────────────────────────────────────────


# A dispatch handler is a callable: (DraftRow) -> (ok: bool, executed_url: Optional[str], reason: Optional[str])
# Pure functions — no shared state — to keep the executor predictable.
DispatchHandler = Callable[[DraftRow], Tuple[bool, Optional[str], Optional[str]]]


def _dispatch_github_comment(row: DraftRow) -> Tuple[bool, Optional[str], Optional[str]]:
    """Post a GitHub issue comment via gh CLI.

    Payload schema: {"body": "..."}
    Target schema:  {"repo": "owner/name", "issue": <int>}
    Returns the resulting comment URL on success.
    """
    repo = row.target.get("repo")
    issue = row.target.get("issue")
    body = (row.payload or {}).get("body") if isinstance(row.payload, dict) else None
    if not (repo and issue and body):
        return False, None, "github_comment requires target.repo, target.issue, payload.body"

    cmd = ["gh", "issue", "comment", str(issue), "--repo", repo, "--body", body]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, None, "gh CLI not found"
    except subprocess.TimeoutExpired:
        return False, None, "gh issue comment timed out"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:300]
        return False, None, f"gh issue comment failed: {stderr}"

    # gh prints the URL on stdout, e.g.
    # "https://github.com/owner/repo/issues/123#issuecomment-..."
    url = (result.stdout or "").strip().splitlines()[-1] if result.stdout else None
    return True, url, None


# ── GitHub autopost path (founder directive 2026-05-03) ─────────────
#
# Founder authorized GitHub autopost alongside X originals. Path:
#   social_post draft (platform=github) → _dispatch_social_post()
#     → _auto_post_github_comment(jsonl_draft_id)
#       → _act_gh_issue_comment / _act_gh_pr_comment from ai.workers.executor
#
# Reuse rationale: ai.workers.executor already exposes typed
# `_act_gh_issue_comment` / `_act_gh_pr_comment` action handlers wrapping
# `gh CLI` with bounded inputs (timeout, output truncation, FileNotFoundError
# handling). Calling those helpers directly preserves the security boundary
# without bridging through the work-order datastore (which would require
# synthesizing a synthetic work order for a draft that already passed the
# inbox-drafts approval flow). The boundary is the helpers themselves: they
# accept only {repo, number, body} and raise ActionError on any deviation.
_GH_AUTOPOST_DAILY_CAP_DEFAULT = 3
_GH_AUTOPOST_AUDIT_PATH = Path.home() / ".delimit" / "workers" / "audit" / "executor.jsonl"
_SOCIAL_DRAFTS_FILE = Path.home() / ".delimit" / "social_drafts.jsonl"


def _gh_autopost_audit(payload: Dict[str, Any]) -> None:
    """Append one audit-log entry for a GH autopost attempt. Best-effort."""
    try:
        _GH_AUTOPOST_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": "inbox_executor.gh_comment_post",
            "action": "gh_comment_post",
            **payload,
        }
        with _GH_AUTOPOST_AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.warning("gh_autopost audit log failed", exc_info=True)


def _gh_autopost_count_today() -> int:
    """Count successful gh_comment_post actions in the current UTC day."""
    if not _GH_AUTOPOST_AUDIT_PATH.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    try:
        for line in _GH_AUTOPOST_AUDIT_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("action") != "gh_comment_post":
                continue
            if not rec.get("posted"):
                continue
            ts = rec.get("ts", "")
            if ts.startswith(today):
                count += 1
    except Exception:
        return 0
    return count


def _parse_github_thread_url(url: str) -> Optional[Tuple[str, int, str]]:
    """Parse a github.com issue/PR URL into (repo, number, kind).

    Accepts:
        https://github.com/owner/name/issues/123
        https://github.com/owner/name/pull/456
        github.com/owner/name/issues/123 (no scheme)

    Returns:
        (repo='owner/name', number=int, kind='issue'|'pr') on success.
        None if the URL is malformed, points to a non-issue/PR path, or
        is not on github.com.
    """
    if not url or not isinstance(url, str):
        return None
    m = re.match(
        r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)(?:[/?#].*)?$",
        url.strip(),
    )
    if not m:
        return None
    owner, name, kind_word, num = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        number = int(num)
    except ValueError:
        return None
    if number <= 0:
        return None
    kind = "pr" if kind_word == "pull" else "issue"
    return f"{owner}/{name}", number, kind


def _load_social_draft(jsonl_draft_id: str) -> Optional[Dict[str, Any]]:
    """Load a single draft entry by draft_id from ~/.delimit/social_drafts.jsonl.

    Returns None if the file is missing or no row matches. Reads only —
    never mutates the JSONL.
    """
    if not _SOCIAL_DRAFTS_FILE.exists():
        return None
    try:
        with _SOCIAL_DRAFTS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("draft_id") == jsonl_draft_id:
                    return row
    except Exception:
        logger.warning("failed to read social_drafts.jsonl", exc_info=True)
        return None
    return None


def _mark_social_draft_posted(jsonl_draft_id: str, url: str) -> None:
    """Write back status=posted on the matched draft row. Best-effort.

    Re-uses ai.social._rewrite_drafts so we don't fight the canonical
    write path (file lock semantics, dedup of registry id, etc.).
    """
    try:
        from ai.social import _load_all_drafts, _rewrite_drafts  # type: ignore
    except Exception:
        logger.warning("ai.social import failed in _mark_social_draft_posted", exc_info=True)
        return
    try:
        all_entries = _load_all_drafts()
        hit = False
        for row in all_entries:
            if row.get("draft_id") == jsonl_draft_id:
                row["status"] = "posted"
                row["posted_at"] = datetime.now(timezone.utc).isoformat()
                row["posted_url"] = url
                hit = True
                break
        if hit:
            _rewrite_drafts(all_entries)
    except Exception:
        logger.warning("failed to mark gh draft posted", exc_info=True)


def _auto_post_github_comment(jsonl_draft_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Post a GitHub issue/PR comment from a social_drafts.jsonl draft.

    Founder directive 2026-05-03 (P0 hotfix): authorized GitHub autopath
    for social_post drafts where target.platform=='github'.

    Safety gates (all must pass):
      1. DELIMIT_ENABLE_GH_AUTOPOST=1 in env (default OFF — fails closed).
      2. Draft must exist and be in {pending, approved} (not cancelled,
         rejected, or already-posted).
      3. Draft must be < 24h old (anti-replay).
      4. Daily cap (default 3, configurable via DELIMIT_GH_AUTOPOST_DAILY_CAP).
      5. thread_url must parse to a github.com issues/<n> or pull/<n> URL.
      6. payload text must be non-empty.

    Returns the executor dispatch contract: (ok, executed_url, reason).
    Every attempt — success or refusal — gets one audit log line at
    ~/.delimit/workers/audit/executor.jsonl with action='gh_comment_post'.
    """
    # Gate 1: env flag (fail-closed default).
    if os.environ.get("DELIMIT_ENABLE_GH_AUTOPOST", "").strip() != "1":
        reason = "DELIMIT_ENABLE_GH_AUTOPOST not enabled (default OFF)"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    # Gate 2: draft exists + status check.
    draft = _load_social_draft(jsonl_draft_id)
    if not draft:
        reason = f"draft '{jsonl_draft_id}' not found in social_drafts.jsonl"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    status = draft.get("status", "")
    if status not in ("pending", "approved"):
        reason = f"draft '{jsonl_draft_id}' has status '{status}', not auto-postable"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    # Gate 3: 24h age check.
    created_iso = draft.get("created_at") or draft.get("timestamp") or ""
    try:
        created_dt = datetime.fromisoformat(str(created_iso).replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0
    except Exception:
        age_hours = 999.0
    if age_hours > 24:
        reason = f"draft '{jsonl_draft_id}' is {age_hours:.1f}h old, exceeds 24h auto-post window"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    # Gate 4: daily rate cap.
    try:
        cap = int(os.environ.get("DELIMIT_GH_AUTOPOST_DAILY_CAP", str(_GH_AUTOPOST_DAILY_CAP_DEFAULT)))
    except ValueError:
        cap = _GH_AUTOPOST_DAILY_CAP_DEFAULT
    today_count = _gh_autopost_count_today()
    if today_count >= cap:
        reason = f"daily gh-autopost cap reached ({today_count}/{cap})"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    # Gate 5: parse the thread URL.
    thread_url = draft.get("thread_url") or ""
    parsed = _parse_github_thread_url(thread_url)
    if not parsed:
        reason = f"draft thread_url is missing or not a parseable github.com issue/PR URL: {thread_url!r}"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason
    repo, number, kind = parsed

    # Gate 6: non-empty body.
    body = (draft.get("text") or "").strip()
    if not body:
        reason = f"draft '{jsonl_draft_id}' has empty text"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    # All gates passed. Dispatch via the existing whitelisted action helper
    # in ai.workers.executor. Lazy-import to avoid module-import-time
    # coupling and to keep this module loadable in a stripped-down deploy.
    try:
        from ai.workers.executor import (
            _act_gh_issue_comment,
            _act_gh_pr_comment,
            ActionError,
        )
    except Exception as e:
        reason = f"ai.workers.executor import failed: {type(e).__name__}: {e}"
        _gh_autopost_audit({"draft_id": jsonl_draft_id, "posted": False, "error": reason})
        return False, None, reason

    handler = _act_gh_pr_comment if kind == "pr" else _act_gh_issue_comment
    params = {"repo": repo, "number": number, "body": body}

    try:
        result = handler(params)
    except ActionError as e:
        reason = f"gh {kind} comment failed: {e}"
        _gh_autopost_audit({
            "draft_id": jsonl_draft_id,
            "posted": False,
            "repo": repo,
            "number": number,
            "kind": kind,
            "error": str(e),
        })
        return False, None, reason
    except Exception as e:
        reason = f"gh {kind} comment raised: {type(e).__name__}: {e}"
        _gh_autopost_audit({
            "draft_id": jsonl_draft_id,
            "posted": False,
            "repo": repo,
            "number": number,
            "kind": kind,
            "error": reason,
        })
        return False, None, reason

    comment_url = result.get("comment_url") if isinstance(result, dict) else None

    # Mark draft posted in the JSONL registry. Best-effort — log failure
    # but don't fail the dispatch (the comment is already posted).
    _mark_social_draft_posted(jsonl_draft_id, comment_url or "")

    # Append to ~/.delimit/social_log.jsonl for parity with X autopost path.
    try:
        social_log = Path.home() / ".delimit" / "social_log.jsonl"
        social_log.parent.mkdir(parents=True, exist_ok=True)
        with social_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "platform": "github",
                "draft_id": jsonl_draft_id,
                "repo": repo,
                "number": number,
                "kind": kind,
                "url": comment_url,
            }) + "\n")
    except Exception:
        logger.debug("social_log append failed (non-fatal)", exc_info=True)

    _gh_autopost_audit({
        "draft_id": jsonl_draft_id,
        "posted": True,
        "repo": repo,
        "number": number,
        "kind": kind,
        "url": comment_url,
    })
    return True, comment_url, None


def _dispatch_social_post(row: DraftRow) -> Tuple[bool, Optional[str], Optional[str]]:
    """Post a social draft via the existing auto-post path — LED-1129 Phase 2.

    Target schema: {platform, account, reply_to_id, thread_url, venture}
    Payload schema: {text, model, fingerprint, metadata: {jsonl_draft_id, ...}}

    Per founder directive 2026-05-03 (P0 hotfix), routing is platform-aware:
      * platform=='twitter': autopost via ai.social.auto_post_draft, which
        applies the originals-only gate (replies/quote-tweets refused).
      * platform=='github':  autopost via _auto_post_github_comment, which
        uses ai.workers.executor's whitelisted gh-comment helpers behind
        DELIMIT_ENABLE_GH_AUTOPOST + 24h age + daily-cap gates.
      * platform=='reddit':  not wired — refuse loudly so founder gets the
        email and posts manually.

    Strategy: the actual posting machinery (rate caps, account allowlist,
    audit log, daily-cap, autopost gate) already lives in
    ai.social.auto_post_draft(jsonl_draft_id). We bridge the registry row
    to that path via metadata.jsonl_draft_id (set when save_draft signs the
    registry entry). All the existing safeguards stay in force; this
    handler is just the executor-side glue.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    target = row.target if isinstance(row.target, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    jsonl_draft_id = metadata.get("jsonl_draft_id")

    if not jsonl_draft_id:
        return False, None, (
            "social_post payload missing metadata.jsonl_draft_id; "
            "cannot bridge to ai.social.auto_post_draft"
        )

    platform = target.get("platform") or "twitter"
    if platform == "reddit":
        # Reddit auto-posting is intentionally not wired — no Reddit API
        # client is configured (Pro users post manually). Fail loudly so
        # the founder gets the email and can post by hand.
        return False, None, (
            "reddit auto-post not implemented; founder posts manually from approval email"
        )

    if platform == "github":
        # Founder directive 2026-05-03: GitHub autopost is allowed (alongside
        # X originals) for social_post drafts on platform=github. Path runs
        # through _auto_post_github_comment, which has its own env flag,
        # 24h age check, daily cap, and audit log entry.
        return _auto_post_github_comment(jsonl_draft_id)

    try:
        from ai.social import auto_post_draft
    except Exception as e:
        return False, None, f"ai.social import failed: {type(e).__name__}: {e}"

    try:
        result = auto_post_draft(jsonl_draft_id)
    except Exception as e:
        return False, None, f"auto_post_draft raised: {type(e).__name__}: {e}"

    if isinstance(result, dict) and result.get("posted"):
        return True, result.get("url"), None

    # Founder directive 2026-05-03 P0 hotfix: auto_post_draft refuses
    # X replies / quote-tweets with status="skipped_manual_required".
    # This is NOT a failure — the draft is terminal-by-policy and the
    # founder will post manually from the email. Surface it as a
    # benign refusal so the executor marks the row completed_with_error
    # (no retry) and logs the policy reason in attempts. We deliberately
    # avoid returning ok=True because the action did not actually post;
    # the COMPLETED_WITH_ERROR transition keeps the contract that
    # "completed" means the side effect ran.
    if isinstance(result, dict) and result.get("status") == "skipped_manual_required":
        reason = result.get("reason") or "skipped_manual_required"
        return False, None, f"skipped_manual_required: {reason}"

    err = (result.get("error") if isinstance(result, dict) else None) or "auto_post_draft returned no posted=True"
    return False, None, f"auto_post_draft failed: {err}"


def _dispatch_unimplemented(row: DraftRow) -> Tuple[bool, Optional[str], Optional[str]]:
    """Placeholder for kinds the executor knows about but hasn't wired yet.

    PR-A ships only github_comment. The other allowlist kinds
    (social_post — wired LED-1129 Phase 2; ledger_done, notify_routing_update,
    deploy_publish_prevalidated_artifact still pending) will be wired in
    subsequent PRs; for now they refuse loudly so the founder isn't
    surprised when "Ship it" doesn't fire on a kind we haven't built yet.
    """
    return False, None, f"dispatch handler for kind={row.draft_kind} not implemented in PR-A"


# Dispatch table. Adding a new key here is itself an authority_class_expansion
# event — the spec doc must be updated and a fresh attestation logged.
DISPATCH_TABLE: Dict[str, DispatchHandler] = {
    "github_comment": _dispatch_github_comment,
    "social_post": _dispatch_social_post,
    "ledger_done": _dispatch_unimplemented,
    "notify_routing_update": _dispatch_unimplemented,
    "deploy_publish_prevalidated_artifact": _dispatch_unimplemented,
}


# ── Failure-notification hook ────────────────────────────────────────


# Decoupled so tests can patch it without spinning up SMTP.
NotifyFn = Callable[[str, str], None]


def _default_notify(subject: str, body: str) -> None:
    """Email founder via delimit_notify with [ALERT] subject prefix.

    Imported lazily so this module doesn't import the entire notify
    surface at startup. Best-effort: failures are logged but don't
    interrupt the executor's main loop.
    """
    try:
        from ai.notify import send_notification
        send_notification(
            channel="email",
            subject=subject,
            message=body,
            event_type="executor_alert",
        )
    except Exception:
        logger.exception("failure-notification hook itself failed; logging only")


# ── Core executor cycle ──────────────────────────────────────────────


def _execute_one(
    row: DraftRow,
    *,
    notify: NotifyFn,
) -> Dict[str, Any]:
    """Process one approved draft. Returns a result dict for diagnostics.

    Order of operations (the at-most-once contract):

    1. Re-verify HMAC + TTL (drafts may have been signed long ago).
    2. Refuse non-delegable kinds.
    3. Atomically transition approved → executing. Lose the race → no-op.
    4. Run the dispatch handler (the actual side effect).
    5. Transition executing → completed (with executed_url) OR
       executing → completed_with_error (with last_error) + email founder.

    A crash between steps 3 and 5 leaves the row stuck at status=executing
    for human reconciliation — we never auto-retry from executing.
    """
    out: Dict[str, Any] = {"draft_id": row.draft_id, "kind": row.draft_kind}

    # Step 1: re-verify
    ok, reason = verify_draft(row.to_signed_dict())
    record_attempt(row.draft_id, kind="verify", outcome=("ok" if ok else "failed"), reason=reason)
    if not ok:
        out["outcome"] = "verify_failed"
        out["reason"] = reason
        # The draft was approved earlier (HMAC was good then) but is no
        # longer verifiable now (TTL elapsed, or signature mismatch from
        # tampering). Mark it terminal; do NOT execute.
        transition(
            row.draft_id,
            expected=DraftStatus.APPROVED.value,
            new=DraftStatus.COMPLETED_WITH_ERROR.value,
            last_error=f"verify failed at execute time: {reason}",
            completed=True,
        )
        notify(
            f"[ALERT] Inbox executor refused {row.draft_id}",
            f"Draft kind={row.draft_kind} failed re-verify at execute time:\n\n{reason}\n\n"
            f"Marked completed_with_error. No retry.",
        )
        return out

    # Step 2: refusal list
    if row.draft_kind in NON_DELEGABLE_REFUSAL_LIST:
        out["outcome"] = "refused_non_delegable"
        out["reason"] = f"{row.draft_kind} is non-delegable per STR-183"
        transition(
            row.draft_id,
            expected=DraftStatus.APPROVED.value,
            new=DraftStatus.TERMINAL_UNRECOVERABLE.value,
            last_error="kind is on the non-delegable refusal list",
            completed=True,
        )
        notify(
            f"[ALERT] Inbox executor refused {row.draft_id}",
            f"Draft kind={row.draft_kind} is on the non-delegable refusal list. "
            f"This action requires fresh per-invocation founder attestation through "
            f"a different channel (not email Ship-it).",
        )
        return out

    # Step 3: take the row atomically. Lose the race → no-op.
    took = transition(
        row.draft_id,
        expected=DraftStatus.APPROVED.value,
        new=DraftStatus.EXECUTING.value,
    )
    if not took:
        out["outcome"] = "lost_race"
        return out

    # Step 4: dispatch
    handler = DISPATCH_TABLE.get(row.draft_kind, _dispatch_unimplemented)
    ok, executed_url, reason = handler(row)
    record_attempt(
        row.draft_id,
        kind="execute",
        outcome=("ok" if ok else "failed"),
        reason=reason,
        executed_url=executed_url,
    )

    # Step 5: terminal transition
    if ok:
        transition(
            row.draft_id,
            expected=DraftStatus.EXECUTING.value,
            new=DraftStatus.COMPLETED.value,
            executed_url=executed_url,
            completed=True,
        )
        out["outcome"] = "executed"
        out["executed_url"] = executed_url
    else:
        transition(
            row.draft_id,
            expected=DraftStatus.EXECUTING.value,
            new=DraftStatus.COMPLETED_WITH_ERROR.value,
            last_error=reason,
            completed=True,
        )
        notify(
            f"[ALERT] Inbox executor failed {row.draft_id}",
            f"Draft kind={row.draft_kind} failed during dispatch:\n\n{reason}\n\n"
            f"Marked completed_with_error. No retry — please re-trigger manually if needed.",
        )
        out["outcome"] = "execute_failed"
        out["reason"] = reason

    return out


def run_cycle(
    *,
    thermal: ThermalState,
    batch_limit: int = 10,
    notify: Optional[NotifyFn] = None,
) -> Dict[str, Any]:
    """One pass of the executor poll loop.

    Picks up to `batch_limit` approved drafts and processes each. Updates
    thermal state on every action. Returns a summary dict suitable for
    logging or status tooling.

    Designed to be safe to call from a 30s scheduler/timer outside this
    process (cron / systemd / supervisor).
    """
    notify_fn = notify or _default_notify

    if thermal.is_paused():
        return {
            "status": "paused",
            "paused_until": thermal.paused_until,
            "processed": 0,
        }

    approved = list_drafts(status=DraftStatus.APPROVED.value, limit=batch_limit)
    if not approved:
        return {"status": "idle", "processed": 0}

    results: List[Dict[str, Any]] = []
    for row in approved:
        if thermal.is_paused():
            results.append({
                "draft_id": row.draft_id,
                "kind": row.draft_kind,
                "outcome": "deferred_thermal",
            })
            continue
        try:
            r = _execute_one(row, notify=notify_fn)
        except Exception as e:
            # Cardinal rule: never let one bad draft kill the loop.
            # The row stays at whatever state we last transitioned it
            # to — likely executing if we crashed inside dispatch —
            # which surfaces it for human reconciliation.
            logger.exception("execute_one raised for %s", row.draft_id)
            r = {
                "draft_id": row.draft_id,
                "kind": row.draft_kind,
                "outcome": "exception",
                "reason": f"{type(e).__name__}: {e}",
            }
        results.append(r)
        # Only count actual side-effect attempts toward thermal, not
        # refusals or verify-failures (those don't reach an external
        # service).
        if r.get("outcome") in {"executed", "execute_failed"}:
            thermal.record()

    return {
        "status": "ran",
        "processed": len(results),
        "results": results,
    }


# ── Daemon control surface ───────────────────────────────────────────


@dataclass
class _ExecutorState:
    """Thread-safe state for the daemon's start/stop/status surface.

    Mirrors inbox_daemon's pattern. Writes to STATE_PATH on every cycle
    so an operator who can't import this module can still cat the file
    and see what's happening.
    """

    running: bool = False
    last_cycle_at: Optional[str] = None
    total_cycles: int = 0
    total_processed: int = 0
    total_executed: int = 0
    total_failed: int = 0
    consecutive_failures: int = 0
    stopped_reason: Optional[str] = None
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    thread: Optional[threading.Thread] = None
    stop_event: Optional[threading.Event] = None
    thermal: Optional[ThermalState] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def to_status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_cycle_at": self.last_cycle_at,
                "total_cycles": self.total_cycles,
                "total_processed": self.total_processed,
                "total_executed": self.total_executed,
                "total_failed": self.total_failed,
                "consecutive_failures": self.consecutive_failures,
                "stopped_reason": self.stopped_reason,
                "poll_interval_seconds": self.poll_interval_seconds,
                "thermal_paused_until": (
                    self.thermal.paused_until if self.thermal else None
                ),
            }

    def persist(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self.to_status_dict(), indent=2))
        except Exception:
            logger.exception("could not persist executor state")


_state = _ExecutorState()


def _daemon_loop(state: _ExecutorState, stop_event: threading.Event) -> None:
    """Run forever (until stop_event), invoking run_cycle each tick.

    Records cycle count + outcome stats on the shared state and writes
    a status file every cycle so external tools can monitor progress.
    """
    while not stop_event.is_set():
        try:
            result = run_cycle(thermal=state.thermal)
            with state._lock:
                state.total_cycles += 1
                state.last_cycle_at = datetime.now(timezone.utc).isoformat()
                if result.get("status") == "ran":
                    state.total_processed += result.get("processed", 0)
                    for r in result.get("results", []):
                        if r.get("outcome") == "executed":
                            state.total_executed += 1
                        elif r.get("outcome") == "execute_failed":
                            state.total_failed += 1
                state.consecutive_failures = 0
        except Exception:
            with state._lock:
                state.consecutive_failures += 1
                state.last_cycle_at = datetime.now(timezone.utc).isoformat()
            logger.exception("inbox_executor cycle raised")
        state.persist()
        # Sleep with early-exit on stop_event.
        if stop_event.wait(timeout=state.poll_interval_seconds):
            break
    with state._lock:
        state.running = False
    state.persist()


def start(
    *,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    thermal_threshold_count: int = 10,
    thermal_threshold_seconds: int = 15 * 60,
    thermal_cooldown_seconds: int = 5 * 60,
) -> Dict[str, Any]:
    """Start the executor daemon thread.

    Idempotent — calling start() on a running daemon returns the same
    status without spawning a second thread. Mirrors the inbox_daemon
    contract so the two control surfaces are operationally symmetric.
    """
    with _state._lock:
        if _state.running:
            return {**_state.to_status_dict(), "action": "already_running"}
        _state.running = True
        _state.stopped_reason = None
        _state.poll_interval_seconds = poll_interval_seconds
        _state.thermal = ThermalState(
            threshold_count=thermal_threshold_count,
            threshold_seconds=thermal_threshold_seconds,
            cooldown_seconds=thermal_cooldown_seconds,
        )
        _state.stop_event = threading.Event()
        _state.thread = threading.Thread(
            target=_daemon_loop,
            args=(_state, _state.stop_event),
            name="inbox_executor",
            daemon=True,
        )
        _state.thread.start()
    _state.persist()
    return {**_state.to_status_dict(), "action": "started"}


def stop(reason: str = "manual") -> Dict[str, Any]:
    """Stop the executor daemon. Idempotent."""
    with _state._lock:
        if not _state.running or not _state.stop_event:
            return {**_state.to_status_dict(), "action": "already_stopped"}
        _state.stop_event.set()
        _state.stopped_reason = reason
        thread = _state.thread
    if thread:
        thread.join(timeout=10.0)
    _state.persist()
    return {**_state.to_status_dict(), "action": "stopped"}


def status() -> Dict[str, Any]:
    """Return current daemon status — does not read SQLite."""
    return _state.to_status_dict()


def control(action: str = "status", **kwargs) -> Dict[str, Any]:
    """Single entry-point matching the delimit_inbox_daemon pattern.

    actions: 'start' (begin polling), 'stop' (halt), 'status' (show state).

    kwargs are forwarded to start() (poll_interval_seconds, thermal
    thresholds). Mostly used by tests to override the defaults.
    """
    action = (action or "status").lower().strip()
    if action == "start":
        return start(**kwargs)
    if action == "stop":
        return stop(**kwargs)
    if action == "status":
        return status()
    return {"error": f"unknown action: {action!r}; use start|stop|status"}


# ── Self-repair reply handler ────────────────────────────────────────
# Self-repair deliberation emails carry an action_id of the form
# `sr-<function>-<kpi>-<ts>` directly in the email body (NOT the SQLite
# drafts registry — self-repair history is its own append-only JSONL
# store). The inbox_daemon detects the action_id + an approval/reject
# keyword in the founder's reply and calls into `handle_self_repair_reply`,
# which is the executor-side bridge that gates+applies the fix.
#
# Convention deviation from the existing draft-kind dispatch table:
# self-repair does not use `DISPATCH_TABLE` or `DraftRow` because the
# fix tiers (prompt_rewrite / kpi_adjust / disable_temp / refusal-of-
# code_change) live behind a constitutional gate (`apply.apply_fix`)
# that already enforces the constitutional refusal list, rate limit,
# and escalation hard-stops. Routing self-repair through the SQLite
# draft pipeline would duplicate the gate logic in two places.


def handle_self_repair_reply(
    *,
    action_id: str,
    action: str,
    reply_text: str,
    notify: Optional[NotifyFn] = None,
) -> Dict[str, Any]:
    """Dispatch a founder reply on a self-repair deliberation email.

    `action` is one of:
      - 'approved'      → run apply.apply_fix on the matched history record.
      - 'rejected'      → mark history record rejected; no fix.
      - 'request_more_info' (or 'more_info' / 'info') → mark history record;
                          schedule a re-deliberation with the founder's
                          note appended. v1 implementation: just records
                          the decision; the watcher's next pass will
                          re-deliberate naturally if the breach persists.

    Returns a dict describing the outcome — designed to be email-back
    friendly so the daemon can confirm the chain to the founder.
    """
    notify_fn = notify or _default_notify
    out: Dict[str, Any] = {
        "action_id": action_id,
        "action": action,
    }

    # Lazy imports keep this module loadable without the self_repair
    # package present (e.g. in stripped-down inbox-only deployments).
    try:
        from ai.self_repair.apply import apply_fix, apply_by_history_id
        from ai.self_repair.history import (
            iter_history,
            update_decision,
        )
        from ai.self_repair.verify import schedule_verify
        from ai.self_repair.kpi import parse_window
    except Exception as exc:
        out["outcome"] = "self_repair_import_failed"
        out["reason"] = f"{type(exc).__name__}: {exc}"
        return out

    # Find the history record by action_id.
    record: Optional[Dict[str, Any]] = None
    for row in iter_history():
        if row.get("action_id") == action_id:
            record = row
            break
    if record is None:
        out["outcome"] = "history_record_not_found"
        notify_fn(
            f"[self-repair] reply for unknown action_id",
            f"Could not match action_id={action_id} (action={action}) "
            f"to any record in self_repair_history.jsonl. "
            f"Reply preview:\n\n{(reply_text or '')[:500]}",
        )
        return out

    norm = (action or "").strip().lower()

    if norm == "approved":
        outcome = apply_fix(
            record,
            {"action": "approved", "reply_text": reply_text},
        )
        out["outcome"] = "applied" if outcome.applied else "apply_refused"
        out["fix_outcome"] = outcome.to_dict()

        # If the apply succeeded AND the fix was a tier we expect to
        # measure (prompt_rewrite / kpi_adjust / disable_temp), schedule
        # a verify. code_change-tier refusals are a no-op for verify
        # since nothing was actually changed.
        if outcome.applied and outcome.fix_tier in (
            "prompt_rewrite",
            "kpi_adjust",
            "disable_temp",
        ):
            try:
                fix_window = _resolve_fix_window(record)
                schedule_verify(
                    history_id=outcome.history_id,
                    fn_name=record.get("function") or "unknown",
                    kpi_name=record.get("breach_kpi") or "unknown",
                    fix_window=fix_window,
                    applied_at=datetime.now(timezone.utc),
                    rollback_token=outcome.rollback_token,
                )
                out["verify_scheduled"] = True
            except Exception as exc:
                out["verify_scheduled"] = False
                out["verify_error"] = f"{type(exc).__name__}: {exc}"

        # Email back the FixOutcome so the founder sees the chain.
        notify_fn(
            f"[self-repair-applied] {record.get('function')} :: "
            f"{record.get('breach_kpi')} — applied={outcome.applied}",
            json.dumps(out["fix_outcome"], indent=2, default=str),
        )
        return out

    if norm == "rejected":
        update_decision(action_id, "rejected")
        out["outcome"] = "rejected"
        notify_fn(
            f"[self-repair-rejected] {record.get('function')} :: "
            f"{record.get('breach_kpi')}",
            f"Founder rejected the proposed fix.\n\n"
            f"Reply preview:\n{(reply_text or '')[:500]}",
        )
        return out

    if norm in ("more_info", "info", "request_more_info"):
        update_decision(action_id, "more_info")
        out["outcome"] = "more_info"
        notify_fn(
            f"[self-repair-more-info] {record.get('function')} :: "
            f"{record.get('breach_kpi')}",
            f"Founder requested more info. The watcher will re-deliberate "
            f"on the next breach detection if the KPI is still failing.\n\n"
            f"Founder note:\n{(reply_text or '')[:1000]}",
        )
        return out

    out["outcome"] = "unknown_action"
    out["reason"] = f"action={action!r} not in approved/rejected/more_info"
    return out


def _resolve_fix_window(record: Dict[str, Any]) -> str:
    """Best-effort recover the KPI window string from a history record.

    The history schema doesn't store the window explicitly; we fall
    back to '24h' if the breach summary doesn't include one. The
    verify scheduler tolerates an unparseable window by defaulting to
    24h, so this is safe.
    """
    # Newer records may carry a `window` in the breach summary. Older
    # ones won't — fall back to 24h.
    summary = record.get("source_data_summary") if isinstance(record, dict) else None
    if isinstance(summary, dict):
        w = summary.get("window")
        if isinstance(w, str) and w.strip():
            return w.strip()
    return str(record.get("window") or "24h")
