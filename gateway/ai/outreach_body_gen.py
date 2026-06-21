"""Outreach body-generation — daemon targets → gate-passing finding drafts.

LED-2214b follow-on (contributions dashboard, 2026-06-19). Closes the gap
between the substantive-outreach DAEMON (which emits *targets to engage* in
``~/.delimit/agents/tasks.json`` with NO comment body) and the inbox-drafts
registry (``~/.delimit/drafts.db``, which holds body-bearing
``github_comment`` drafts the dashboard can approve).

What this module does, per dispatched ``outreach_substantive`` target:

  1. Read the target's repo + proposed_action + evidence_refs from the
     daemon's tasks.json record.
  2. Run the Delimit diff engine on the target spec's before/after versions
     (reuses ``scripts/outreach_report_generator`` helpers — same GitHub-API
     + diff path the public reports use).
  3. Draft a PURE-TECHNICAL finding body: what changed, which are breaking,
     where (file path + PR). Zero Delimit promo — naming the product, a
     delimit.ai URL, or a "we built" phrase would be blocked by the gate
     downstream, and the body-gen never writes such a body.
  4. Validate the body against ``check_substantive_content`` BEFORE writing.
     A body that does not PASS the gate is never persisted — we log and skip.
  5. Sign + insert a PENDING ``github_comment`` draft into drafts.db, keyed
     by the target fingerprint so re-runs don't duplicate.

Design intent (why a body the gate ALLOWS, by construction):
  * The gate requires (a) ≥ MIN_BODY_LENGTH chars, (b) at least one technical
    anchor (commit / issue / CVE / spec path / file path), (c) NO covert
    commercial content. The diff engine hands us file paths + change messages
    for free, so a faithful summary of the diff is anchored and substantive
    without any promo. We assert the gate verdict before persisting; if a
    future template change ever introduces a forbidden token, the body is
    dropped rather than shipped.

This module does NOT post anything. It only produces drafts. Posting happens
later, founder-approved, through the dashboard → proxy → inbox_executor path,
where the gate is re-run server-side a SECOND time immediately before the gh
call (defense in depth — the dashboard is convenience, the gate is the law).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.outreach_body_gen")

# Resolve gateway root so the report-generator import works whether we're run
# in-process (server.py) or as a script.
_GATEWAY_ROOT = Path(
    os.environ.get("DELIMIT_GATEWAY_ROOT", "/home/delimit/delimit-gateway")
)
if str(_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT))

_AGENT_TASKS_FILE = Path(
    os.environ.get(
        "DELIMIT_AGENT_TASKS_FILE",
        os.path.expanduser("~/.delimit/agents/tasks.json"),
    )
)

# How many recent merged PRs to scan per target looking for spec changes.
_MAX_PRS_SCAN = 15
# Cap distinct change messages embedded in a finding body so a huge diff
# doesn't produce a 40KB comment.
_MAX_CHANGE_LINES = 12


# ── tasks.json reader (mirrors proxy server _outreach_queue filter) ──────────


def _load_dispatched_targets() -> List[Dict[str, Any]]:
    """Return the dispatched outreach_substantive tasks from tasks.json.

    Each returned dict is the task's ``variables`` block plus ``task_id``.
    Empty list on any failure (missing file, malformed JSON).
    """
    if not _AGENT_TASKS_FILE.exists():
        return []
    try:
        data = json.loads(_AGENT_TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("tasks.json unreadable: %s", exc)
        return []

    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        tasks = data["tasks"]
    elif isinstance(data, dict):
        tasks = [v for v in data.values() if isinstance(v, dict)]
    elif isinstance(data, list):
        tasks = data
    else:
        return []

    out: List[Dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if t.get("task_type") != "outreach_substantive":
            continue
        if t.get("status") not in {"dispatched", "in_progress", "open"}:
            continue
        v = dict(t.get("variables") or {})
        v["task_id"] = t.get("task_id") or t.get("id") or ""
        out.append(v)
    return out


def _target_fingerprint(target_vars: Dict[str, Any]) -> str:
    """Stable key for dedup: prefer the daemon's own fingerprint, else
    derive one from repo + target_artifact."""
    cand = target_vars.get("candidate") or {}
    fp = (
        target_vars.get("source_fingerprint")
        or (cand.get("fingerprint") if isinstance(cand, dict) else None)
        or target_vars.get("fingerprint")
    )
    if fp:
        return str(fp)
    repo = target_vars.get("repo") or (cand.get("repo") if isinstance(cand, dict) else "")
    art = target_vars.get("target_artifact") or (
        cand.get("target_artifact") if isinstance(cand, dict) else ""
    )
    return f"github:{repo}:{art}"


def _resolve_target_field(target_vars: Dict[str, Any], key: str, default: str = "") -> str:
    """Read a field from the task variables, falling back to the nested
    ``candidate`` block (the daemon stores some fields in both places)."""
    if target_vars.get(key):
        return str(target_vars[key])
    cand = target_vars.get("candidate")
    if isinstance(cand, dict) and cand.get(key):
        return str(cand[key])
    return default


# ── diff-driven finding generation ───────────────────────────────────────────


def _diff_target_spec(repo: str) -> Dict[str, Any]:
    """Run the public report-generator diff path on ``repo``.

    Returns a dict:
      {
        "ok": bool,
        "spec_paths": [...],
        "prs": [ {number, title, url, breaking, non_breaking, change_messages} ],
        "total_changes": int, "breaking": int, "non_breaking": int,
        "reason": str (when ok is False),
      }

    Read-only: hits the GitHub API via the gh CLI (as the configured gh
    account) the same way the public reports do. No posting.
    """
    try:
        from scripts.outreach_report_generator import (
            resolve_spec_paths,
            get_recent_merged_prs,
            get_pr_files,
            get_file_at_ref,
            parse_spec_content,
            run_diff,
            is_spec_file,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"report_generator import failed: {exc}"}

    spec_paths = resolve_spec_paths(repo, "")
    if not spec_paths:
        return {"ok": False, "reason": "no_spec_paths_found", "spec_paths": []}

    prs = get_recent_merged_prs(repo, limit=_MAX_PRS_SCAN)
    if not prs:
        return {"ok": False, "reason": "no_merged_prs", "spec_paths": spec_paths}

    out_prs: List[Dict[str, Any]] = []
    total = breaking = non_breaking = 0

    for pr in prs:
        pr_number = pr.get("number")
        base_sha = pr.get("base", {}).get("sha", "")
        merge_sha = pr.get("merge_commit_sha", "")
        if not (pr_number and base_sha and merge_sha):
            continue

        files = get_pr_files(repo, pr_number)
        spec_files = [
            f for f in files
            if f.get("filename", "") in spec_paths or is_spec_file(f.get("filename", ""))
        ]
        if not spec_files:
            continue

        pr_msgs: List[str] = []
        pr_breaking = pr_non = 0
        for sf in spec_files:
            filename = sf["filename"]
            old_content = get_file_at_ref(repo, filename, base_sha)
            new_content = get_file_at_ref(repo, filename, merge_sha)
            if old_content is None and new_content is None:
                continue
            old_spec = parse_spec_content(old_content, filename) if old_content else {}
            new_spec = parse_spec_content(new_content, filename) if new_content else {}
            old_spec = old_spec or {}
            new_spec = new_spec or {}
            try:
                changes = run_diff(old_spec, new_spec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("diff failed for %s %s: %s", repo, filename, exc)
                continue
            for ch in changes:
                total += 1
                if getattr(ch, "is_breaking", False):
                    breaking += 1
                    pr_breaking += 1
                    pr_msgs.append(f"[BREAKING] {ch.message}  (in `{filename}`)")
                else:
                    non_breaking += 1
                    pr_non += 1
                    pr_msgs.append(f"{ch.message}  (in `{filename}`)")

        if pr_msgs:
            out_prs.append({
                "number": pr_number,
                "title": pr.get("title", ""),
                "url": pr.get("html_url", f"https://github.com/{repo}/pull/{pr_number}"),
                "breaking": pr_breaking,
                "non_breaking": pr_non,
                "change_messages": pr_msgs,
            })

    if total == 0:
        return {"ok": False, "reason": "no_spec_changes", "spec_paths": spec_paths}

    return {
        "ok": True,
        "spec_paths": spec_paths,
        "prs": out_prs,
        "total_changes": total,
        "breaking": breaking,
        "non_breaking": non_breaking,
    }


def _compose_finding_body(repo: str, diff: Dict[str, Any]) -> str:
    """Build a pure-technical finding body from the diff result.

    The body is deliberately product-neutral: it states what changed and
    which changes are breaking, anchored to the file path(s) and PR number(s)
    the diff came from. It never names Delimit, links delimit.ai, or uses a
    "we built / our tool" phrase — those are gate violations and the gate is
    re-asserted before this body is ever persisted.
    """
    spec_list = ", ".join(f"`{p}`" for p in diff.get("spec_paths", [])) or "the OpenAPI spec"
    breaking = diff.get("breaking", 0)
    non_breaking = diff.get("non_breaking", 0)
    total = diff.get("total_changes", 0)

    lines: List[str] = []
    lines.append(
        f"While reviewing recent changes to {spec_list} in this repository, "
        f"I found {total} API-surface change(s) across recently merged pull "
        f"requests — {breaking} of which look backward-incompatible for "
        f"existing clients and {non_breaking} backward-compatible."
    )
    lines.append("")

    if breaking:
        lines.append(
            "The backward-incompatible changes are worth calling out because "
            "they can break consumers that pin to the current schema without a "
            "major-version bump:"
        )
        lines.append("")

    shown = 0
    for pr in diff.get("prs", []):
        if shown >= _MAX_CHANGE_LINES:
            break
        msgs = pr.get("change_messages", [])
        # surface breaking first
        msgs_sorted = sorted(msgs, key=lambda m: not m.startswith("[BREAKING]"))
        header_added = False
        for m in msgs_sorted:
            if shown >= _MAX_CHANGE_LINES:
                break
            if not header_added:
                lines.append(f"- PR #{pr['number']} ({pr['url']}):")
                header_added = True
            lines.append(f"  - {m}")
            shown += 1

    remaining = total - shown
    if remaining > 0:
        lines.append("")
        lines.append(f"(… and {remaining} further change(s) in the same diff.)")

    lines.append("")
    lines.append(
        "Flagging in case the backward-incompatible items were intentional and "
        "already covered by a versioning policy — if not, a changelog note or a "
        "semver-major signal would help downstream consumers migrate. Happy to "
        "share the per-endpoint diff if useful."
    )
    return "\n".join(lines)


# ── gate validation + draft persistence ──────────────────────────────────────


def _gate_body(body: str, proposed_action: str, repo: str) -> Dict[str, Any]:
    """Run the bright-line substantive-content gate. Returns the verdict dict."""
    from ai.outreach_substantive import check_substantive_content
    return check_substantive_content(body, proposed_action, repo=repo)


def _already_drafted(fingerprint: str) -> bool:
    """True if a non-terminal github_comment draft already exists for this
    target fingerprint (dedup so repeated body-gen runs don't pile up)."""
    try:
        from ai.inbox_drafts import list_drafts
    except Exception:  # noqa: BLE001
        return False
    try:
        rows = list_drafts(limit=200)
    except Exception:  # noqa: BLE001
        return False
    for r in rows:
        if r.draft_kind != "github_comment":
            continue
        if r.status in {"cancelled", "completed", "terminal_unrecoverable"}:
            continue
        payload = r.payload if isinstance(r.payload, dict) else {}
        if payload.get("source_fingerprint") == fingerprint:
            return True
    return False


def _persist_draft(
    repo: str,
    issue_or_pr_number: Optional[int],
    body: str,
    proposed_action: str,
    fingerprint: str,
    led_ref: Optional[str],
) -> Dict[str, Any]:
    """Sign + insert a PENDING github_comment draft. Returns a result dict."""
    from ai.inbox_drafts import sign_draft, insert_draft

    target: Dict[str, Any] = {"repo": repo}
    if issue_or_pr_number is not None:
        target["issue"] = issue_or_pr_number

    # The fingerprint + proposed_action ride along in the payload so the
    # dashboard / approve path can re-derive context and dedup, WITHOUT
    # changing the github_comment payload contract the executor reads
    # (it only requires payload.body).
    payload: Dict[str, Any] = {
        "body": body,
        "source_fingerprint": fingerprint,
        "proposed_action": proposed_action,
    }

    signed = sign_draft("github_comment", target, payload)
    try:
        insert_draft(signed, led_ref=led_ref)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"insert_failed: {exc}", "draft_id": signed.draft_id}
    return {"ok": True, "draft_id": signed.draft_id, "repo": repo, "action": proposed_action}


def _parse_artifact_number(target_artifact: str) -> Optional[int]:
    """Pull the trailing issue/PR number out of a github URL, if present."""
    import re
    m = re.search(r"/(?:issues|pull)/(\d+)", target_artifact or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def generate_drafts(
    *,
    max_targets: int = 5,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Turn dispatched daemon targets into PENDING body-bearing drafts.

    For each dispatched outreach_substantive target (capped at
    ``max_targets`` — the daemon's own per-day cap is 5):

      1. Skip if a live draft already exists for the fingerprint.
      2. Run the diff engine on the target repo's spec.
      3. Compose a pure-technical finding body.
      4. ASSERT the gate ALLOWS the body. If blocked, skip + log (never
         persist a body the gate rejects).
      5. Persist a PENDING draft (unless dry_run).

    Returns a summary dict with per-target outcomes. Never posts.
    """
    targets = _load_dispatched_targets()
    results: List[Dict[str, Any]] = []
    drafted = skipped = blocked = 0

    for tv in targets:
        if drafted >= max_targets:
            break
        repo = _resolve_target_field(tv, "repo")
        proposed_action = _resolve_target_field(tv, "proposed_action", "comment")
        target_artifact = _resolve_target_field(tv, "target_artifact") or _resolve_target_field(
            tv, "source_url"
        )
        fingerprint = _target_fingerprint(tv)
        task_id = tv.get("task_id", "")

        rec: Dict[str, Any] = {
            "task_id": task_id,
            "repo": repo,
            "proposed_action": proposed_action,
            "fingerprint": fingerprint,
        }

        if not repo or "/" not in repo:
            rec["outcome"] = "skipped_bad_repo"
            skipped += 1
            results.append(rec)
            continue

        if _already_drafted(fingerprint):
            rec["outcome"] = "skipped_existing_draft"
            skipped += 1
            results.append(rec)
            continue

        if proposed_action not in ("comment", "issue", "pr"):
            proposed_action = "comment"

        diff = _diff_target_spec(repo)
        if not diff.get("ok"):
            rec["outcome"] = "skipped_no_diff"
            rec["reason"] = diff.get("reason")
            skipped += 1
            results.append(rec)
            continue

        # LED-3493: only surface findings that carry at least one
        # backward-INCOMPATIBLE change. An all-backward-compatible diff has
        # nothing actionable for the maintainer — drafting it produces
        # low-value noise on a third-party repo, and the body's "in case the
        # backward-incompatible items were intentional" ask is incongruent
        # when there are zero. Suppress entirely (the dashboard should only
        # ever show genuinely useful, specific findings).
        if int(diff.get("breaking", 0) or 0) <= 0:
            rec["outcome"] = "skipped_no_breaking"
            rec["reason"] = (
                f"{int(diff.get('total_changes', 0) or 0)} change(s), "
                "all backward-compatible"
            )
            skipped += 1
            results.append(rec)
            continue

        body = _compose_finding_body(repo, diff)

        verdict = _gate_body(body, proposed_action, repo)
        rec["gate"] = {"verdict": verdict.get("verdict"), "reason": verdict.get("reason")}
        if verdict.get("verdict") != "allow":
            # By construction the template should pass; if it ever doesn't,
            # we DROP the body rather than persist a non-compliant draft.
            rec["outcome"] = "blocked_by_gate"
            rec["violations"] = verdict.get("violations")
            blocked += 1
            results.append(rec)
            logger.warning(
                "body-gen body blocked by gate for %s: %s",
                repo, verdict.get("reason"),
            )
            continue

        if dry_run:
            rec["outcome"] = "would_draft"
            rec["body_preview"] = body[:300]
            drafted += 1
            results.append(rec)
            continue

        number = _parse_artifact_number(target_artifact)
        persisted = _persist_draft(
            repo, number, body, proposed_action, fingerprint, led_ref=task_id or None
        )
        if persisted.get("ok"):
            rec["outcome"] = "drafted"
            rec["draft_id"] = persisted["draft_id"]
            drafted += 1
        else:
            rec["outcome"] = "persist_failed"
            rec["reason"] = persisted.get("reason")
            skipped += 1
        results.append(rec)

    return {
        "drafted": drafted,
        "skipped": skipped,
        "blocked": blocked,
        "total_targets": len(targets),
        "dry_run": dry_run,
        "results": results,
    }


if __name__ == "__main__":  # pragma: no cover - manual debugging entrypoint
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(generate_drafts(max_targets=args.max, dry_run=args.dry_run), indent=2, default=str))
