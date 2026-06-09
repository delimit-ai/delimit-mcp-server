"""
Delimit Ledger Manager — Strategy + Operational ledger as first-class MCP tools.

Two ledgers per project:
- Strategy: consensus decisions, positioning, pricing, product direction
- Operational: tasks, bugs, features — the "keep building" items

Ledger lives at {project}/.delimit/ledger/ (project-local).
Ventures auto-registered at ~/.delimit/ventures.json on first use.
"""

import base64
import json
import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

GLOBAL_DIR = Path.home() / ".delimit"
VENTURES_FILE = GLOBAL_DIR / "ventures.json"

# LED-1145 Phase 2 #3: P0 quota soft warning. The soft block fires when an
# add_item call would push the unresolved-P0 count over the quota. Item is
# still added — this is policy nudge, not enforcement, per the strategic
# deliberation's hierarchy ("policy after primitives"). Override via env var:
#   DELIMIT_P0_SOFT_QUOTA=80     # raise the gate
#   DELIMIT_P0_SOFT_QUOTA=0      # disable warning entirely
P0_SOFT_QUOTA_DEFAULT = 50


def _p0_soft_quota() -> int:
    """Resolve the active P0 quota threshold from env. 0 disables warnings."""
    raw = os.environ.get("DELIMIT_P0_SOFT_QUOTA", "")
    if raw == "":
        return P0_SOFT_QUOTA_DEFAULT
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return P0_SOFT_QUOTA_DEFAULT


def _count_unresolved_p0(project_path: str = ".") -> int:
    """Count P0 items currently in an unresolved state (open / in_progress /
    blocked) across both ops and strategy ledgers. Excludes done / cancelled /
    archived. Used by the add_item soft-quota nudge."""
    ledger_dir = _project_ledger_dir(project_path)
    unresolved = {"open", "in_progress", "blocked"}
    count = 0
    for filename in ("operations.jsonl", "strategy.jsonl"):
        path = ledger_dir / filename
        if not path.exists():
            continue
        items = _read_ledger(path)
        # Replay events to current state
        state: Dict[str, Dict[str, Any]] = {}
        for item in items:
            iid = item.get("id", "")
            if not iid:
                continue
            if item.get("type") == "update":
                if iid in state:
                    if "status" in item:
                        state[iid]["status"] = item["status"]
                    if "priority" in item:
                        state[iid]["priority"] = item["priority"]
            else:
                state[iid] = {**item}
        for it in state.values():
            if it.get("priority") == "P0" and it.get("status") in unresolved:
                count += 1
    return count


def _detect_venture(project_path: str = ".") -> Dict[str, str]:
    """Auto-detect venture/project info from the directory."""
    p = Path(project_path).resolve()
    info = {"name": p.name, "path": str(p)}

    # Try package.json
    pkg = p / "package.json"
    if pkg.exists():
        try:
            d = json.loads(pkg.read_text())
            info["name"] = d.get("name", p.name)
            info["type"] = "node"
        except Exception:
            pass

    # Try pyproject.toml
    pyproj = p / "pyproject.toml"
    if pyproj.exists():
        try:
            text = pyproj.read_text()
            for line in text.splitlines():
                if line.strip().startswith("name"):
                    name = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if name:
                        info["name"] = name
                        info["type"] = "python"
                        break
        except Exception:
            pass

    # Try git remote
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, cwd=str(p)
        )
        if remote.returncode == 0:
            url = remote.stdout.strip()
            # Extract repo name from URL
            repo = url.rstrip("/").split("/")[-1].replace(".git", "")
            info["repo"] = url
            if not info.get("type"):
                info["name"] = repo
    except Exception:
        pass

    return info


def _register_venture(info: Dict[str, str]):
    """Silently register a venture in the global registry.

    Phase C follow-up (2026-05-18): reject paths under /tmp/* or the
    bare "/tmp" itself. Pytest tmp_path values leaked into the registry
    as ventures (`tmp: /tmp`, `test_project: /tmp/pytest-of-root/...`),
    causing every fresh tmp_path to match via path-prefix in
    resolve_venture and breaking test_resolve_venture_unregistered_path.
    The guard fails-silently — tests that pass tmp_path to functions
    which auto-register simply don't pollute the registry going forward.
    """
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    ventures = {}
    if VENTURES_FILE.exists():
        try:
            ventures = json.loads(VENTURES_FILE.read_text())
        except Exception:
            pass

    name = info["name"]
    path = info.get("path", "")
    # Guard against the specific test-state pollution that broke
    # test_resolve_venture_unregistered_path: a `tmp: /tmp` venture
    # caught EVERY pytest tmp_path via path-prefix in resolve_venture.
    # Reject bare "/tmp" only. Deeper /tmp/<X> paths are fine — they
    # only path-prefix-match their own subtree, not every tmp_path,
    # AND legitimate test fixtures (e.g. test_ledger_proof) register
    # subpaths during a single test run and need that to work.
    if path == "/tmp" or path.rstrip("/") == "/tmp":
        return
    if name not in ventures:
        ventures[name] = {
            "path": path,
            "repo": info.get("repo", ""),
            "type": info.get("type", ""),
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        VENTURES_FILE.write_text(json.dumps(ventures, indent=2))


# LED-1188 / Plan-C: env-aware home so DELIMIT_HOME / DELIMIT_NAMESPACE_ROOT
# overrides apply to the ledger paths same as everywhere else. Falls back
# to ~/.delimit when neither env var is set (back-compat with v4.5.1 and
# all prior versions).
def _delimit_home() -> Path:
    for env_key in ("DELIMIT_HOME", "DELIMIT_NAMESPACE_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val)
    return Path.home() / ".delimit"


CENTRAL_LEDGER_DIR = _delimit_home() / "ledger"
LEDGER_V2_DIR = _delimit_home() / "ledger-v2"

# LED-1188 D3 (deliberation att_f86e1f51110e8ed6 follow-up, 2026-04-28):
# Plan-C migration partitions the central ledger into per-venture sub-ledgers
# under ledger-v2/<slug>/. The resolver below auto-detects which layout is
# present and reads from it. Slugs match the migration script's canonical
# names so a v4.5.2 install picks up an existing Plan-C-staged tree without
# requiring the swap to happen first.
_VENTURE_CANONICAL = {
    "delimit-mcp": "delimit",
    "delimit-action": "delimit",
    "delimit-ui": "delimit",
    "delimit-cli": "delimit",        # npm package name
    "delimit-gateway": "delimit",    # gateway repo
    ".delimit": "delimit",
    "wirereport": "wire-report",
    "stakeone": "stake-one",
}
_KNOWN_VENTURE_SLUGS = {
    "delimit", "wire-report", "domainvested",
    "livetube", "stake-one", "root", "unsorted",
}


def _canonical_venture_slug(name: str) -> Optional[str]:
    """Map a detected venture name to a canonical sub-ledger slug.

    Returns None when the name doesn't match any known venture; callers
    treat that as "no per-venture sub-ledger, use the central layout."
    """
    if not name:
        return None
    n = name.lower().strip()
    n = _VENTURE_CANONICAL.get(n, n)
    return n if n in _KNOWN_VENTURE_SLUGS else None


def _detect_model() -> str:
    """Auto-detect which AI model is running this session.

    Checks environment variables set by various AI coding assistants:
    - CLAUDE_MODEL / CLAUDE_CODE_MODEL: Claude Code
    - CODEX_MODEL: OpenAI Codex CLI
    - GEMINI_MODEL: Gemini CLI
    - MCP_CLIENT_NAME: Generic MCP client identifier
    Falls back to "unknown" if none are set.
    """
    # Claude Code
    for var in ("CLAUDE_MODEL", "CLAUDE_CODE_MODEL"):
        val = os.environ.get(var)
        if val:
            return val

    # OpenAI Codex
    val = os.environ.get("CODEX_MODEL")
    if val:
        return val

    # Gemini
    val = os.environ.get("GEMINI_MODEL")
    if val:
        return val

    # Generic MCP client
    val = os.environ.get("MCP_CLIENT_NAME")
    if val:
        return val

    return "unknown"


def _project_ledger_dir(project_path: str = ".") -> Path:
    """Resolve the ledger directory for a project, with Plan-C auto-detect.

    Resolution order (LED-1188 D3, deliberation att_f86e1f51110e8ed6):
      1. Detect venture from project_path -> canonical slug (delimit,
         wire-report, domainvested, livetube, stake-one).
      2. If LEDGER_V2_DIR / <slug> / operations.jsonl exists, return that
         per-venture sub-ledger. (Plan-C staged but not yet swapped.)
      3. If CENTRAL_LEDGER_DIR / <slug> / operations.jsonl exists, return
         that per-venture sub-ledger. (Plan-C swapped.)
      4. Fall back to CENTRAL_LEDGER_DIR (legacy single-file layout).

    Cross-model handoff fix (still enforced): Codex and Gemini were writing to
    $PWD/.delimit/ledger/ which caused ledger fragmentation. The central
    ~/.delimit/ledger/ tree (or its Plan-C-partitioned form) remains the
    single source of truth — per-project .delimit/ dirs are for policies and
    config only.
    """
    # Quick exit: legacy callers that pass venture="" / project="." and
    # have no Plan-C tree on disk get the original single-file layout.
    if not LEDGER_V2_DIR.exists() and not (CENTRAL_LEDGER_DIR / "delimit").exists():
        return CENTRAL_LEDGER_DIR

    info = _detect_venture(project_path)
    slug = _canonical_venture_slug(info.get("name", ""))
    if slug is None:
        return CENTRAL_LEDGER_DIR

    # Plan-C staged: ledger-v2/<slug>/
    staged = LEDGER_V2_DIR / slug
    if (staged / "operations.jsonl").exists():
        return staged

    # Plan-C swapped: ledger/<slug>/
    swapped = CENTRAL_LEDGER_DIR / slug
    if (swapped / "operations.jsonl").exists():
        return swapped

    # No partitioned tree for this venture — fall back to the central
    # legacy layout (operations.jsonl + strategy.jsonl directly in ledger/).
    return CENTRAL_LEDGER_DIR


def _ensure(project_path: str = "."):
    ledger_dir = _project_ledger_dir(project_path)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    for name in ["strategy.jsonl", "operations.jsonl"]:
        f = ledger_dir / name
        if not f.exists():
            f.write_text("")

    # Auto-register venture on first use
    info = _detect_venture(project_path)
    _register_venture(info)


def _read_ledger(path: Path) -> List[Dict]:
    items = []
    if not path.exists():
        return items
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _append(path: Path, entry: Dict) -> Dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("")
    items = _read_ledger(path)
    prev_hash = items[-1].get("hash", "genesis") if items else "genesis"
    entry["hash"] = hashlib.sha256(f"{prev_hash}{json.dumps(entry, sort_keys=True)}".encode()).hexdigest()[:16]
    entry["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ── LED-877 signal guard ─────────────────────────────────────────────


# Sources that originate from sensed observations (social/strategy scans).
# Centralized so the guard logic is in one place even if more prefixes are
# added later (e.g. github_sense, reddit_sense).
_SENSED_SOURCE_PREFIXES = ("social_scan", "social_strategy")


def _check_source_is_ledger_item(
    source: str,
    *,
    purpose: str = "promote_to_ledger",
    title: str = "",
    ledger: str = "",
) -> None:
    """LED-877 signal guard.

    Sensed observations (``source='social_scan:...'``) MUST NOT land in
    the ledger by default — they belong in the intel signal store.

    LED-216 Phase 1 split: callers can declare *why* they are checking.

    ``purpose='promote_to_ledger'`` (default, original strict behavior)
        Used by ``add_item`` and any code path that actually writes a
        ledger row. Raises ``ValueError`` on a sensed source unless the
        ``_DELIMIT_SIGNAL_PROMOTED_BY`` bypass env var is set (which is
        the explicit promote-to-ledger path).

    ``purpose='draft_only'``
        Used by code paths that produce a reply draft from a sensed
        observation but do NOT promote the signal to the ledger. Drafts
        are an acceptable consumer of sensed sources, so the guard is a
        no-op for this purpose.

    Both purposes still respect ``DELIMIT_SIGNAL_GUARD=shadow`` for the
    shadow-log fallback used during the LED-877 rollout.
    """
    _src_norm = (source or "").strip().lower()
    if not any(_src_norm.startswith(p) for p in _SENSED_SOURCE_PREFIXES):
        return  # Not a sensed source; nothing to guard against.

    if purpose == "draft_only":
        # Drafts may legitimately reference a sensed observation. The
        # guard exists to prevent ledger writes, not draft generation.
        return

    if purpose != "promote_to_ledger":
        # Defensive: unknown purpose ⇒ default to strict behavior so a
        # typo can't accidentally weaken the guard.
        pass

    _promoted_by = os.environ.get("_DELIMIT_SIGNAL_PROMOTED_BY", "")
    if _promoted_by:
        return  # Explicit promote_to_ledger path; bypass authorized.

    msg = (
        f"LED-877 guard: source={source!r} is a sensed observation, not "
        f"a ledger item. Use ai.sensing.signal_store.ingest() instead. "
        f"Promote explicitly via promote_to_ledger(signal_id=...)."
    )

    _guard_mode = os.environ.get("DELIMIT_SIGNAL_GUARD", "enforce").lower()
    if _guard_mode == "shadow":
        try:
            _shadow_log = Path.home() / ".delimit" / "logs" / "signal_guard_shadow.jsonl"
            _shadow_log.parent.mkdir(parents=True, exist_ok=True)
            with _shadow_log.open("a") as _f:
                _f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "title": title,
                    "source": source,
                    "ledger": ledger,
                    "purpose": purpose,
                    "msg": msg,
                }) + "\n")
        except Exception:
            pass
        # fall through (shadow mode does not raise)
        return

    raise ValueError(msg)


def add_item(
    title: str,
    ledger: str = "ops",
    type: str = "task",
    priority: str = "P1",
    description: str = "",
    source: str = "session",
    project_path: str = ".",
    tags: Optional[List[str]] = None,
    acceptance_criteria: Optional[List[str]] = None,
    context: str = "",
    tools_needed: Optional[List[str]] = None,
    estimated_complexity: str = "",
    worked_by: str = "",
) -> Dict[str, Any]:
    """Add a new item to the project's strategy or operational ledger.

    LED-189: Items can have acceptance_criteria (testable "done when" conditions).
    LED-190: Items can have context, tools_needed, and estimated_complexity
    for agent-executable task format.
    LED-877: Signal guard — rejects source='social_scan' writes so sensed
    observations cannot land in the ledger. Observations belong in the intel
    signal store (ai/sensing/signal_store.py). Bypass via env var for the
    promote_to_ledger path: _DELIMIT_SIGNAL_PROMOTED_BY=<who>.

    LED-216 Phase 1: the guard is now reusable via
    ``_check_source_is_ledger_item(..., purpose='draft_only')`` for code
    paths that produce reply drafts from sensed observations without
    promoting the underlying signal to the ledger.
    """
    _check_source_is_ledger_item(
        source,
        purpose="promote_to_ledger",
        title=title,
        ledger=ledger,
    )

    _ensure(project_path)
    venture = _detect_venture(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    path = ledger_dir / ("strategy.jsonl" if ledger == "strategy" else "operations.jsonl")

    # LED-824: ID-collision fix. The Plan-C resolver routes delimit-context
    # queries to ledger-v2/<slug>/. Per-venture ID counters used to scan
    # only the active sub-ledger, so newly-created items could collide with
    # IDs already used in the legacy CENTRAL_LEDGER_DIR root files. Now we
    # union all known IDs across (a) the resolved sub-ledger AND (b) every
    # peer sub-ledger AND (c) the legacy root, then pick the next free.
    items = _read_ledger(path)
    prefix = "STR" if ledger == "strategy" else "LED"
    existing_ids = {i.get("id", "") for i in items if i.get("type") != "update"}

    # Union with all peer files in ledger-v2/* and the legacy root files,
    # for both strategy and operations ledgers (an LED-N could collide
    # whether it lives in operations or strategy in any sub-ledger).
    filename = "strategy.jsonl" if ledger == "strategy" else "operations.jsonl"
    candidate_paths: list[Path] = []
    if LEDGER_V2_DIR.exists():
        for sub in LEDGER_V2_DIR.iterdir():
            if sub.is_dir():
                candidate_paths.append(sub / filename)
    candidate_paths.append(CENTRAL_LEDGER_DIR / filename)

    for cand in candidate_paths:
        if cand == path:
            continue  # already scanned
        if not cand.exists():
            continue
        try:
            for entry in _read_ledger(cand):
                if entry.get("type") == "update":
                    continue
                eid = entry.get("id", "")
                if eid:
                    existing_ids.add(eid)
        except Exception:
            # Best-effort: a malformed peer file shouldn't block id assignment
            continue

    # Walk forward from len()+1 until we find a non-colliding slot.
    num = len(items) + 1
    while f"{prefix}-{num:03d}" in existing_ids:
        num += 1
    item_id = f"{prefix}-{num:03d}"

    entry = {
        "id": item_id,
        "title": title,
        "type": type,
        "priority": priority,
        "description": description,
        "source": source,
        "venture": venture["name"],
        "status": "open",
        "tags": tags or [],
        "worked_by": worked_by or _detect_model(),
    }
    # LED-189: Optional acceptance criteria
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    # LED-190: Optional agent-executable fields
    if context:
        entry["context"] = context
    if tools_needed:
        entry["tools_needed"] = tools_needed
    if estimated_complexity:
        entry["estimated_complexity"] = estimated_complexity

    result = _append(path, entry)

    # Sync to Supabase for dashboard visibility
    try:
        from ai.supabase_sync import sync_ledger_item
        sync_ledger_item(result)
    except Exception:
        pass  # Never let cloud sync break ledger operations

    response: Dict[str, Any] = {
        "added": result,
        "ledger": ledger,
        "venture": venture["name"],
        "total_items": len(_read_ledger(path)),
    }

    # LED-1145 Phase 2 #3: P0 soft quota nudge. Soft (item still added),
    # not hard. Surfaces a warning when the unresolved-P0 count crosses
    # the quota — gives the founder a signal to groom before piling on.
    if priority == "P0":
        quota = _p0_soft_quota()
        if quota > 0:
            current_p0 = _count_unresolved_p0(project_path)
            if current_p0 > quota:
                response["warning"] = (
                    f"P0 quota soft-block: {current_p0} unresolved P0 items "
                    f"(threshold {quota}). Item was still added. Consider running "
                    f"delimit_ledger_groom to triage existing P0s before adding more, "
                    f"or set DELIMIT_P0_SOFT_QUOTA={current_p0 + 50} to raise the gate."
                )
                response["p0_count"] = current_p0
                response["p0_quota"] = quota

    return response


def _find_item_in_ledger_dir(item_id: str, ledger_dir: Path) -> Optional[Dict[str, Any]]:
    """Search a ledger directory for an item by ID. Returns (ledger_name, path) or None."""
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        path = ledger_dir / filename
        items = _read_ledger(path)
        for item in items:
            if item.get("id") == item_id and item.get("type") != "update":
                return {"ledger_name": ledger_name, "path": path}
    return None


def update_item(
    item_id: str,
    status: Optional[str] = None,
    note: Optional[str] = None,
    priority: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    due_date: Optional[str] = None,
    labels: Optional[List[str]] = None,
    blocked_by: Optional[str] = None,
    blocks: Optional[str] = None,
    project_path: str = ".",
    worked_by: str = "",
    commit_sha: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing ledger item's fields.

    LED-1408 Phase 1: when `status="done"` is requested, callers MAY provide
    `commit_sha` and/or `pr_url` as proof that the work shipped to main.
    The proof is recorded on the update event under `ship_proof` with a
    `verified: bool` flag. Phase 1 does NOT enforce — items still
    transition to `done` even without proof — but the flag lets future
    audits and the Phase 2 reconciler find unverified-done items.
    """
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)

    # First, search the specified project's ledger
    found = _find_item_in_ledger_dir(item_id, ledger_dir)

    # If not found, search all registered ventures as a fallback
    if not found:
        try:
            ventures = {}
            if VENTURES_FILE.exists():
                ventures = json.loads(VENTURES_FILE.read_text())
        except Exception:
            ventures = {}

        searched = {str(ledger_dir)}
        for _name, info in ventures.items():
            vpath = info.get("path", "")
            if not vpath:
                continue
            candidate_dir = Path(vpath) / ".delimit" / "ledger"
            if str(candidate_dir) in searched:
                continue
            searched.add(str(candidate_dir))
            found = _find_item_in_ledger_dir(item_id, candidate_dir)
            if found:
                break

    if found:
        ledger_name = found["ledger_name"]
        path = found["path"]
        update = {
            "id": item_id,
            "type": "update",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "worked_by": worked_by or _detect_model(),
        }
        if status:
            update["status"] = status
        if note:
            update["note"] = note
        if priority:
            update["priority"] = priority
        if title:
            update["title"] = title
        if description:
            update["description"] = description
        if assignee:
            update["assignee"] = assignee
        if due_date:
            update["due_date"] = due_date
        if labels is not None:
            # LED-2221: write to both `labels` and `tags`. The list_items
            # reconstruction (around line ~870) merges update events into
            # current state by checking the `tags` key only. Writing only
            # `labels` silently drops the update at read time, which in
            # particular meant the build daemon's `autonomous-build` tag
            # check could never see tags written through the MCP. Keeping
            # `labels` for any external consumer that reads the raw event
            # stream; adding `tags` so the live state aggregator picks it up.
            update["labels"] = labels
            update["tags"] = labels
        if blocked_by:
            update["blocked_by"] = blocked_by
        if blocks:
            update["blocks"] = blocks

        # LED-1408 Phase 1: attach ship_proof block when status transitions to
        # `done` or `shipped_pending`. Verified=True iff commit_sha or pr_url
        # was supplied (directly or scraped from the note). Phase 2's
        # reconciler will use this to distinguish "trustworthy done" from
        # "marked done but never verified on main."
        if status in ("done", "shipped_pending"):
            try:
                from ai.ledger_proof import build_ship_proof
                update["ship_proof"] = build_ship_proof(
                    commit_sha=commit_sha,
                    pr_url=pr_url,
                    note=note,
                )
                # LED-1420 Phase 2 strict-mode flip: when DELIMIT_LEDGER_STRICT_DONE=1,
                # an unverified `done` transition is downgraded to `shipped_pending`
                # so the nightly reconciler (scripts/delimit_ledger_reconciler.py)
                # can promote it to `done` once a commit-trailer match shows up on
                # origin/main. Off by default so existing workflows keep closing
                # items without hitting an unexpected gate; flip when the
                # reconciler has been observed running for ~1 week without
                # surprises.
                strict = os.environ.get("DELIMIT_LEDGER_STRICT_DONE") == "1"
                if (
                    strict
                    and status == "done"
                    and not update["ship_proof"].get("verified")
                ):
                    update["status"] = "shipped_pending"
                    existing_note = update.get("note") or ""
                    suffix = (
                        "[LED-1420 strict-mode: downgraded done → shipped_pending — "
                        "no commit_sha/pr_url proof; reconciler will upgrade to "
                        "done when it finds a Ledger-Item: " + item_id + " trailer "
                        "on origin/main]"
                    )
                    update["note"] = (existing_note + " " + suffix).strip() if existing_note else suffix
            except Exception:
                # Soft-fail: a ship_proof bug must not break ledger close.
                # The unverified state will be re-detectable from the missing
                # key on the next audit pass.
                pass

        _append(path, update)

        # Sync to Supabase for dashboard visibility
        try:
            from ai.supabase_sync import sync_ledger_update
            sync_ledger_update(item_id, status=status or "", note=note or "")
        except Exception:
            pass  # Never let cloud sync break ledger operations

        return {"updated": item_id, "changes": update, "ledger": ledger_name}

    return {"error": f"Item {item_id} not found in project ledger"}


# LED-1145 Phase 1 PR-A: known-good slim projection for AI agent triage.
# Strips description / acceptance_criteria / context / tags / hash etc. so the
# response fits in an MCP tool result without truncation. Default behaviour
# stays full (callers depending on description still get it); pass
# fields="slim" or an explicit allowlist to opt in.
SLIM_FIELDS = ("id", "title", "status", "priority", "type", "venture", "updated_at")
_VALID_FIELDS = SLIM_FIELDS + (
    "description", "acceptance_criteria", "context", "tags", "created_at",
    "worked_by", "last_worked_by", "last_note", "hash", "source", "tools_needed",
    "estimated_complexity", "ledger",
)
_VALID_SORT = ("updated_at", "created_at", "priority")
_VALID_ORDER = ("asc", "desc")
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _normalize_filter_list(value):
    """Accept None / str / list and produce a list of strings (or None)."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value) if value else None


def _resolve_fields(fields):
    """Map the `fields` parameter to a concrete projection set.

    Returns:
        (projection: set[str] | None, error: str | None)
        - projection=None means "return all fields" (backward-compat default)
        - error is set when an unknown field name was requested
    """
    if fields is None or fields == "":
        return None, None
    if isinstance(fields, str):
        if fields == "slim":
            return set(SLIM_FIELDS), None
        if fields == "*":
            return None, None
        # comma-separated string from the MCP boundary
        names = [f.strip() for f in fields.split(",") if f.strip()]
    else:
        names = list(fields)
    if not names:
        return None, None
    if names == ["*"]:
        return None, None
    if names == ["slim"]:
        return set(SLIM_FIELDS), None
    unknown = [n for n in names if n not in _VALID_FIELDS and n != "*"]
    if unknown:
        return None, f"unknown field(s) requested: {sorted(unknown)}; valid: {sorted(_VALID_FIELDS)}"
    return set(names), None


def _ts_to_iso(value):
    """Coerce a timestamp value into a comparable ISO string. Empty → ''."""
    return value or ""


def _compare_iso(a: str, b: str, op: str) -> bool:
    """Lexical ISO comparison; both sides must be naively-comparable strings.
    Empty string sorts before any real timestamp so '<X' is True for missing."""
    if op == "before":
        return a < b
    if op == "after":
        return a > b
    return False


def _make_cursor(offset: int, filter_sig: str) -> str:
    payload = f"{offset}:{filter_sig}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        offset_str, filter_sig = decoded.split(":", 1)
        return int(offset_str), filter_sig
    except (ValueError, UnicodeDecodeError):
        return 0, ""


def _filter_signature(spec: Dict[str, Any]) -> str:
    """Short, deterministic hash of the filter spec; used to invalidate cursors
    when the caller changes filters between page requests."""
    canonical = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def list_items(
    ledger: str = "both",
    # Backward-compat single-value filters:
    status: Optional[str] = None,
    priority: Optional[str] = None,
    # New multi-value filters:
    status__in=None,
    priority__in=None,
    tags__contains_all=None,
    text: Optional[str] = None,
    linked_external_id: Optional[str] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    updated_before: Optional[str] = None,
    updated_after: Optional[str] = None,
    # Sort + projection + pagination:
    sort: str = "updated_at",
    order: str = "desc",
    fields=None,
    limit: int = 50,
    cursor: Optional[str] = None,
    project_path: str = ".",
) -> Dict[str, Any]:
    """List ledger items with optional filters, sort, projection, and cursor pagination.

    LED-1145 Phase 1 PR-A: extended from the original 3-filter signature.
    Backward compatible — old callers passing only `status` / `priority`
    continue to work without change.

    Args:
        ledger: "ops" | "strategy" | "both".
        status: single-value status filter (back-compat).
        priority: single-value priority filter (back-compat).
        status__in: list (or comma-separated string) of statuses to match.
        priority__in: list (or comma-separated string) of priorities to match.
        tags__contains_all: list (or comma-separated string); item must contain ALL these tags.
        text: case-insensitive substring match against title + description.
        linked_external_id: exact substring match in description or tags
            (e.g. "github.com/owner/repo/issues/123").
        created_before / created_after / updated_before / updated_after:
            ISO timestamps (e.g. "2026-04-01T00:00:00Z"). Lexical compare.
        sort: "updated_at" | "created_at" | "priority". Default updated_at.
        order: "asc" | "desc". Default desc.
        fields: response projection. None or "*" = full (default, back-compat).
            "slim" = SLIM_FIELDS only. List/CSV of field names = those only.
            Unknown field names → ERROR (no silent no-op).
        limit: page size (default 50).
        cursor: opaque pagination token from a prior call's `next_cursor`.
            If filters change between calls, the cursor is invalidated and the
            response begins at offset 0 with `cursor_invalidated=True`.
        project_path: ledger root (auto-detect when ".").

    Returns:
        {
            "venture": str,
            "items": {"ops": [...], "strategy": [...]},
            "summary": {"total": int, "open": int, "done": int, "in_progress": int},
            "next_cursor": str | None,
            "cursor_invalidated": bool (only when True),
        }
    """
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    venture = _detect_venture(project_path)

    if sort not in _VALID_SORT:
        return {"error": f"sort must be one of {list(_VALID_SORT)}"}
    if order not in _VALID_ORDER:
        return {"error": f"order must be one of {list(_VALID_ORDER)}"}

    projection, projection_err = _resolve_fields(fields)
    if projection_err:
        return {"error": projection_err}

    # Normalise filter list params (accept str / list / None)
    status_list = _normalize_filter_list(status__in)
    priority_list = _normalize_filter_list(priority__in)
    tags_list = _normalize_filter_list(tags__contains_all)

    # Backward compat: status="open" → status_list=["open"]; same for priority.
    if status and not status_list:
        status_list = [status]
    if priority and not priority_list:
        priority_list = [priority]

    # Filter signature for cursor invalidation
    filter_spec = {
        "ledger": ledger,
        "status_list": sorted(status_list) if status_list else None,
        "priority_list": sorted(priority_list) if priority_list else None,
        "tags_list": sorted(tags_list) if tags_list else None,
        "text": text,
        "linked_external_id": linked_external_id,
        "created_before": created_before,
        "created_after": created_after,
        "updated_before": updated_before,
        "updated_after": updated_after,
        "sort": sort,
        "order": order,
    }
    current_sig = _filter_signature(filter_spec)

    # Decode cursor if provided
    start_offset = 0
    cursor_invalidated = False
    if cursor:
        decoded_offset, decoded_sig = _decode_cursor(cursor)
        if decoded_sig == current_sig:
            start_offset = decoded_offset
        else:
            cursor_invalidated = True

    text_lower = (text or "").lower() if text else None

    results: Dict[str, list] = {}
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        if ledger not in ("both", ledger_name):
            continue

        path = ledger_dir / filename
        items = _read_ledger(path)

        # Build current state by replaying events (event-sourced)
        state: Dict[str, Dict[str, Any]] = {}
        for item in items:
            item_id = item.get("id", "")
            if item.get("type") == "update":
                if item_id in state:
                    if "status" in item:
                        state[item_id]["status"] = item["status"]
                    if "note" in item:
                        state[item_id]["last_note"] = item["note"]
                    if "priority" in item:
                        state[item_id]["priority"] = item["priority"]
                    if "worked_by" in item:
                        state[item_id]["last_worked_by"] = item["worked_by"]
                    if "tags" in item and item["tags"] is not None:
                        # Tag updates replace the existing tag set when present
                        state[item_id]["tags"] = item["tags"]
                    state[item_id]["updated_at"] = item.get("updated_at")
            else:
                state[item_id] = {**item}

        filtered = list(state.values())

        # Apply filters
        if status_list:
            statuses = set(status_list)
            filtered = [i for i in filtered if i.get("status") in statuses]
        if priority_list:
            priorities = set(priority_list)
            filtered = [i for i in filtered if i.get("priority") in priorities]
        if tags_list:
            required_tags = set(tags_list)
            filtered = [
                i for i in filtered
                if required_tags.issubset(set(i.get("tags") or []))
            ]
        if text_lower:
            filtered = [
                i for i in filtered
                if text_lower in (i.get("title") or "").lower()
                or text_lower in (i.get("description") or "").lower()
            ]
        if linked_external_id:
            needle = linked_external_id
            filtered = [
                i for i in filtered
                if needle in (i.get("description") or "")
                or needle in " ".join(i.get("tags") or [])
                or needle in (i.get("context") or "")
            ]
        if created_before:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("created_at")), created_before, "before")]
        if created_after:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("created_at")), created_after, "after")]
        if updated_before:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("updated_at") or i.get("created_at")), updated_before, "before")]
        if updated_after:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("updated_at") or i.get("created_at")), updated_after, "after")]

        # Sort
        reverse = order == "desc"
        if sort == "priority":
            filtered.sort(
                key=lambda x: (
                    _PRIORITY_ORDER.get(x.get("priority", "P2"), 9),
                    x.get("created_at", ""),
                ),
                reverse=reverse,
            )
        else:
            sort_key = "updated_at" if sort == "updated_at" else "created_at"
            filtered.sort(
                key=lambda x: x.get(sort_key) or x.get("created_at") or "",
                reverse=reverse,
            )

        results[ledger_name] = filtered

    # Apply projection + pagination across the combined result.
    # Combine per-ledger lists in stable order (ops first, then strategy).
    combined: list = []
    for ledger_name in ("ops", "strategy"):
        if ledger_name in results:
            for it in results[ledger_name]:
                # Tag each item with its ledger source so the projected
                # response retains the routing info even when "ledger" itself
                # isn't part of the original record.
                if "ledger" not in it:
                    it = {**it, "ledger": ledger_name}
                combined.append(it)

    total_pre_page = len(combined)
    page = combined[start_offset:start_offset + limit]
    next_offset = start_offset + len(page)
    has_more = next_offset < total_pre_page
    next_cursor = _make_cursor(next_offset, current_sig) if has_more else None

    # Apply projection to page items.
    if projection is not None:
        page = [{k: v for k, v in i.items() if k in projection} for i in page]

    # Re-bucket projected page items back into ops / strategy for response shape.
    paged_results: Dict[str, list] = {"ops": [], "strategy": []}
    if ledger != "strategy":
        paged_results.setdefault("ops", [])
    if ledger != "ops":
        paged_results.setdefault("strategy", [])
    # Walk the page using the still-tagged combined data to know which bucket;
    # we kept "ledger" in the projection step only when it was already there.
    for src, dst in zip(combined[start_offset:start_offset + limit], page):
        bucket = src.get("ledger") or "ops"
        paged_results.setdefault(bucket, []).append(dst)

    summary_total = total_pre_page
    response = {
        "venture": venture["name"],
        "items": {k: v for k, v in paged_results.items() if k in results},
        "summary": {
            "total": summary_total,
            "open": sum(1 for i in combined if i.get("status") == "open"),
            "done": sum(1 for i in combined if i.get("status") == "done"),
            "in_progress": sum(1 for i in combined if i.get("status") == "in_progress"),
            "blocked": sum(1 for i in combined if i.get("status") == "blocked"),
            "archived": sum(1 for i in combined if i.get("status") == "archived"),
        },
        "next_cursor": next_cursor,
    }
    if cursor_invalidated:
        response["cursor_invalidated"] = True
    return response


def get_context(project_path: str = ".") -> Dict[str, Any]:
    """Get a concise ledger summary for AI context — what's open, what's next."""
    venture = _detect_venture(project_path)
    result = list_items(status="open", project_path=project_path)
    open_items = []
    for ledger_items in result["items"].values():
        open_items.extend(ledger_items)

    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    open_items.sort(key=lambda x: priority_order.get(x.get("priority", "P2"), 9))

    return {
        "venture": venture["name"],
        "open_items": len(open_items),
        "next_up": [{"id": i["id"], "title": i["title"], "priority": i["priority"]}
                     for i in open_items[:5]],
        "summary": result["summary"],
    }


def list_ventures() -> Dict[str, Any]:
    """List all registered ventures/projects."""
    if not VENTURES_FILE.exists():
        return {"ventures": {}, "count": 0}
    try:
        ventures = json.loads(VENTURES_FILE.read_text())
        return {"ventures": ventures, "count": len(ventures)}
    except Exception:
        return {"ventures": {}, "count": 0}


# ═══════════════════════════════════════════════════════════════════════
#  LEDGER QUERY (Natural language → structured queries)
# ═══════════════════════════════════════════════════════════════════════

def query_ledger(query: str, project_path: str = ".") -> Dict[str, Any]:
    """Answer natural language questions about the ledger.

    Supports: "what shipped this week?", "what's blocked?", "show P0s",
    "how many items completed?", "what's next?", etc.
    """
    q = query.lower().strip()

    # Route to appropriate data based on query intent
    if any(w in q for w in ["shipped", "completed", "done", "finished", "closed"]):
        result = list_items(status="done", project_path=project_path, limit=50)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)

        # Filter by time if mentioned
        if "today" in q:
            today = time.strftime("%Y-%m-%d")
            items = [i for i in items if i.get("updated_at", "").startswith(today) or i.get("created_at", "").startswith(today)]
        elif "week" in q or "7 day" in q:
            cutoff = time.time() - 7 * 86400
            items = [i for i in items if _parse_ts(i.get("updated_at", "")) > cutoff]
        elif "month" in q or "30 day" in q:
            cutoff = time.time() - 30 * 86400
            items = [i for i in items if _parse_ts(i.get("updated_at", "")) > cutoff]

        return {"query": query, "intent": "completed", "items": [{"id": i["id"], "title": i["title"]} for i in items], "count": len(items)}

    elif any(w in q for w in ["blocked", "blocking", "stuck"]):
        result = list_items(status="open", project_path=project_path, limit=50)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        # Check for items with blocked_by links
        blocked = []
        for i in items:
            links = get_links(i["id"], project_path)
            has_blocker = any(l.get("type") == "blocked_by" for l in links.get("links", []))
            if has_blocker or i.get("status") == "blocked":
                blocked.append(i)
        return {"query": query, "intent": "blocked", "items": [{"id": i["id"], "title": i["title"]} for i in blocked], "count": len(blocked)}

    elif any(w in q for w in ["next", "should i", "what to work", "priority", "urgent"]):
        return get_context(project_path)

    elif "p0" in q:
        result = list_items(priority="P0", project_path=project_path, limit=20)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        return {"query": query, "intent": "priority_filter", "priority": "P0", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status", "open")} for i in items], "count": len(items)}

    elif "p1" in q:
        result = list_items(priority="P1", project_path=project_path, limit=20)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        return {"query": query, "intent": "priority_filter", "priority": "P1", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status", "open")} for i in items], "count": len(items)}

    elif any(w in q for w in ["how many", "count", "total", "stats", "summary"]):
        result = list_items(project_path=project_path, limit=500)
        all_items = []
        for v in result.get("items", {}).values():
            all_items.extend(v)
        by_status = {}
        by_priority = {}
        by_venture = {}
        for i in all_items:
            s = i.get("status", "open")
            by_status[s] = by_status.get(s, 0) + 1
            p = i.get("priority", "P1")
            by_priority[p] = by_priority.get(p, 0) + 1
            v = i.get("venture", "unknown")
            by_venture[v] = by_venture.get(v, 0) + 1
        return {"query": query, "intent": "stats", "total": len(all_items), "by_status": by_status, "by_priority": by_priority, "by_venture": by_venture}

    elif any(w in q for w in ["open", "todo", "remaining", "left"]):
        return get_context(project_path)

    else:
        # Default: search by keyword in titles
        result = list_items(project_path=project_path, limit=100)
        all_items = []
        for v in result.get("items", {}).values():
            all_items.extend(v)
        words = q.split()
        matches = [i for i in all_items if any(w in i.get("title", "").lower() for w in words)]
        return {"query": query, "intent": "search", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status")} for i in matches[:20]], "count": len(matches)}


def _parse_ts(ts_str: str) -> float:
    """Parse ISO timestamp to epoch seconds."""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════
#  LEDGER LINKS (Dependencies, Blockers, Parent-Child)
# ═══════════════════════════════════════════════════════════════════════

LINKS_FILE_NAME = "links.jsonl"
VALID_LINK_TYPES = {"blocks", "blocked_by", "parent", "child", "relates_to", "duplicates", "supersedes", "superseded_by"}


def link_items(
    from_id: str,
    to_id: str,
    link_type: str = "blocks",
    note: str = "",
    project_path: str = ".",
) -> Dict[str, Any]:
    """Create a relationship between two ledger items."""
    if link_type not in VALID_LINK_TYPES:
        return {"error": f"Invalid link_type '{link_type}'. Use: {', '.join(sorted(VALID_LINK_TYPES))}"}

    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    link = {
        "from": from_id,
        "to": to_id,
        "type": link_type,
        "note": note,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(links_file, "a") as f:
        f.write(json.dumps(link) + "\n")

    # Auto-create reverse link for bidirectional types
    reverse_map = {
        "blocks": "blocked_by",
        "blocked_by": "blocks",
        "parent": "child",
        "child": "parent",
        "supersedes": "superseded_by",
        "superseded_by": "supersedes",
    }
    if link_type in reverse_map:
        reverse = {
            "from": to_id,
            "to": from_id,
            "type": reverse_map[link_type],
            "note": note,
            "created_at": link["created_at"],
            "auto_reverse": True,
        }
        with open(links_file, "a") as f:
            f.write(json.dumps(reverse) + "\n")

    return {"linked": True, "from": from_id, "to": to_id, "type": link_type}


def get_links(
    item_id: str,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Get all links/relationships for a ledger item."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    if not links_file.exists():
        return {"item_id": item_id, "links": [], "count": 0}

    links = []
    try:
        for line in links_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            link = json.loads(line)
            if link.get("from") == item_id or link.get("to") == item_id:
                links.append(link)
    except Exception:
        pass

    return {"item_id": item_id, "links": links, "count": len(links)}


def unlink_items(
    from_id: str,
    to_id: str,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Remove all links between two items."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    if not links_file.exists():
        return {"unlinked": False, "reason": "No links file"}

    kept = []
    removed = 0
    for line in links_file.read_text().strip().split("\n"):
        if not line.strip():
            continue
        link = json.loads(line)
        if (link.get("from") == from_id and link.get("to") == to_id) or \
           (link.get("from") == to_id and link.get("to") == from_id):
            removed += 1
        else:
            kept.append(line)

    links_file.write_text("\n".join(kept) + "\n" if kept else "")
    return {"unlinked": True, "removed": removed}


# ═══════════════════════════════════════════════════════════════════════
#  SESSION HANDOFF
# ═══════════════════════════════════════════════════════════════════════

SESSIONS_DIR = GLOBAL_DIR / "sessions"


def session_handoff(
    summary: str,
    items_completed: Optional[List[str]] = None,
    items_added: Optional[List[str]] = None,
    key_decisions: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    files_changed: Optional[List[str]] = None,
    venture: str = "",
) -> Dict[str, Any]:
    """Store a session summary for cross-session continuity.

    Called at end of a productive session so the next session can load context.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = f"session_{time.strftime('%Y%m%d_%H%M%S')}"
    handoff = {
        "id": session_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "venture": venture or "all",
        "summary": summary,
        "items_completed": items_completed or [],
        "items_added": items_added or [],
        "key_decisions": key_decisions or [],
        "blockers": blockers or [],
        "files_changed": files_changed or [],
    }

    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(handoff, indent=2))

    # LED-1705: stamp the deterministic-floor coordinator so the Stop hook
    # treats this model-invoked handoff as the fresh, richer artifact and
    # skips writing a deterministic floor over it.
    try:
        try:
            from ai.last_capture import stamp_capture
        except ImportError:  # pragma: no cover - flat import layout
            from last_capture import stamp_capture
        stamp_capture(source="model", session_id=session_id)
    except Exception:
        pass

    return {"saved": session_id, "path": str(path), "handoff": handoff}


def session_history(limit: int = 5) -> Dict[str, Any]:
    """Load recent session handoffs for context recovery."""
    if not SESSIONS_DIR.exists():
        return {"sessions": [], "count": 0}

    files = sorted(SESSIONS_DIR.glob("session_*.json"), reverse=True)[:limit]
    sessions = []
    for f in files:
        try:
            sessions.append(json.loads(f.read_text()))
        except Exception:
            continue

    return {"sessions": sessions, "count": len(sessions)}


# ── LED-1145 Phase 1 PR-B: bulk_action ───────────────────────────────────

# Single allowlisted enum keeps the API surface tiny and predictable.
# `archive` is a soft transition (status="archived", appended to JSONL); items
# stay in replay forever. NO hard delete. Per-item failures don't block others.
BULK_ACTIONS = ("archive", "set_status", "set_priority", "add_tag", "mark_done", "cancel")
# LED-1408: `shipped_pending` is the intermediate state between "committed" and
# "verified on main." Items transition to shipped_pending when a worker reports
# completion (commit exists somewhere) but the orchestrator hasn't yet verified
# the commit is reachable from origin/main. The reconciler (Phase 2) promotes
# shipped_pending → done once reachability is confirmed.
_VALID_BULK_STATUSES = ("open", "in_progress", "blocked", "shipped_pending", "done", "cancelled", "archived", "completed")
_VALID_BULK_PRIORITIES = ("P0", "P1", "P2", "P3")


def _normalize_id_list(value):
    """Accept None / str (CSV) / list and return list[str]. Used by bulk_action
    and the MCP tool wrapper."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _replay_current_state(item_id: str, ledger_dir: Path) -> Optional[Dict[str, Any]]:
    """Walk the ledger and return the CURRENT replayed state of `item_id`,
    or None if the item doesn't exist. Used by bulk_action for dry-run
    diff preview AND by auto_close_linked_external to verify state changes.

    Replays the same fields list_items does so callers see a consistent view."""
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        path = ledger_dir / filename
        items = _read_ledger(path)
        state: Optional[Dict[str, Any]] = None
        for item in items:
            if item.get("id") != item_id:
                continue
            if item.get("type") == "update":
                if state is not None:
                    if "status" in item:
                        state["status"] = item["status"]
                    if "priority" in item:
                        state["priority"] = item["priority"]
                    if "tags" in item and item["tags"] is not None:
                        state["tags"] = item["tags"]
                    if "note" in item:
                        state["last_note"] = item["note"]
                    if "worked_by" in item:
                        state["last_worked_by"] = item["worked_by"]
                    if "updated_at" in item:
                        state["updated_at"] = item["updated_at"]
            else:
                state = {**item}
        if state is not None:
            return state
    return None


def bulk_action(
    item_ids,
    action: str,
    dry_run: bool = True,
    note: Optional[str] = None,
    new_status: Optional[str] = None,
    new_priority: Optional[str] = None,
    tag: Optional[str] = None,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Apply one action to many items. Default `dry_run=True` returns what
    would change without writing. Per-item failures are reported but don't
    block other items in the batch.

    LED-1145 Phase 1 PR-B. The deliberation locked these defaults: a single
    enum-dispatched tool (not bulk_close/bulk_done/...), dry_run=True default,
    no hard delete, archive is a soft status transition.

    Args:
        item_ids: list of LED-XXX ids (str or list-of-str; CSV string accepted).
        action: one of BULK_ACTIONS.
        dry_run: when True (default), don't write. When False, apply.
        note: optional note attached to every successful update event.
        new_status: required when action="set_status".
        new_priority: required when action="set_priority".
        tag: required when action="add_tag" (single tag string).
        project_path: ledger root.

    Returns:
        {
            "dry_run": bool,
            "action": str,
            "would_change": [{id, field, old, new}, ...]  # if dry_run
            "changed":      [{id, field, old, new}, ...]  # if not dry_run
            "errors":       [{id, reason}]
            "summary": {"requested": int, "would_change": int (or "changed"), "errors": int}
        }
    """
    if action not in BULK_ACTIONS:
        return {
            "error": f"unknown action {action!r}; allowed: {list(BULK_ACTIONS)}",
        }

    ids = _normalize_id_list(item_ids)
    if not ids:
        return {"error": "item_ids must contain at least one id"}

    # Per-action argument validation
    if action == "set_status":
        if not new_status:
            return {"error": "set_status requires new_status"}
        if new_status not in _VALID_BULK_STATUSES:
            return {"error": f"new_status must be one of {list(_VALID_BULK_STATUSES)}"}
    if action == "set_priority":
        if not new_priority:
            return {"error": "set_priority requires new_priority"}
        if new_priority not in _VALID_BULK_PRIORITIES:
            return {"error": f"new_priority must be one of {list(_VALID_BULK_PRIORITIES)}"}
    if action == "add_tag":
        if not tag or not str(tag).strip():
            return {"error": "add_tag requires a non-empty tag"}

    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)

    # Build the per-item change description.
    # archive    → status: <current> → archived
    # set_status → status: <current> → <new_status>
    # set_priority → priority: <current> → <new_priority>
    # add_tag    → tags: [..current..] → [..current.., <tag>] (if not already present)
    # mark_done  → status: <current> → done
    # cancel     → status: <current> → cancelled
    changes: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for item_id in ids:
        state = _replay_current_state(item_id, ledger_dir)
        if state is None:
            errors.append({"id": item_id, "reason": "not_found"})
            continue

        if action in ("archive", "mark_done", "cancel", "set_status"):
            new_val = {
                "archive": "archived",
                "mark_done": "done",
                "cancel": "cancelled",
                "set_status": new_status,
            }[action]
            old_val = state.get("status")
            if old_val == new_val:
                # No-op; skip silently (idempotent action). Don't record.
                continue
            changes.append({"id": item_id, "field": "status", "old": old_val, "new": new_val})
        elif action == "set_priority":
            old_val = state.get("priority")
            if old_val == new_priority:
                continue
            changes.append({"id": item_id, "field": "priority", "old": old_val, "new": new_priority})
        elif action == "add_tag":
            existing_tags = state.get("tags") or []
            if tag in existing_tags:
                continue
            changes.append({
                "id": item_id, "field": "tags",
                "old": list(existing_tags),
                "new": list(existing_tags) + [tag],
            })

    if dry_run:
        return {
            "dry_run": True,
            "action": action,
            "would_change": changes,
            "errors": errors,
            "summary": {
                "requested": len(ids),
                "would_change": len(changes),
                "errors": len(errors),
            },
        }

    # Apply mode: write one update event per change.
    applied: List[Dict[str, Any]] = []
    for change in changes:
        item_id = change["id"]
        field = change["field"]
        try:
            if field == "status":
                update_item(item_id=item_id, status=change["new"], note=note, project_path=project_path)
            elif field == "priority":
                update_item(item_id=item_id, priority=change["new"], note=note, project_path=project_path)
            elif field == "tags":
                _apply_tag_update(item_id, change["new"], note=note, project_path=project_path)
            applied.append(change)
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            errors.append({"id": item_id, "reason": f"write_failed: {exc}"})

    return {
        "dry_run": False,
        "action": action,
        "changed": applied,
        "errors": errors,
        "summary": {
            "requested": len(ids),
            "changed": len(applied),
            "errors": len(errors),
        },
    }


def _apply_tag_update(item_id: str, new_tags: List[str], note: Optional[str], project_path: str) -> None:
    """Append a tags-update event for an item. Used by bulk_action(add_tag).

    update_item() doesn't support a tags param today, so we write the update
    event directly through the same path it uses. The replay logic in
    list_items already handles `tags` updates.
    """
    ledger_dir = _project_ledger_dir(project_path)
    found = _find_item_in_ledger_dir(item_id, ledger_dir)
    if not found:
        raise RuntimeError(f"item {item_id} disappeared during apply")
    path = found["path"]
    update_event = {
        "id": item_id,
        "type": "update",
        "tags": new_tags,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if note:
        update_event["note"] = note
    _append(path, update_event)


# ── LED-1145 Phase 2 #1: linked-external auto-close ──────────────────────

import re as _re

# Long form: https://github.com/<owner>/<repo>/(issues|pull)/<num>
_GH_URL_RE = _re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)")
# Short form: <owner>/<repo>#<num> (avoid matching plain markdown headings)
_GH_SHORT_RE = _re.compile(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)\b")
# Explicit tag form: gh:<owner>/<repo>/<num> — for callers that want unambiguous linkage
_GH_TAG_RE = _re.compile(r"^gh:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(\d+)$")


def _extract_external_link(item: Dict[str, Any]) -> Optional[tuple]:
    """Find the FIRST github issue/PR reference inside an item.

    Scans description, context, last_note, and tags (in that order). Returns
    `(owner, repo, number)` or None. Long URLs > short forms > explicit tags.
    """
    haystacks = []
    for field in ("description", "context", "last_note"):
        v = item.get(field)
        if isinstance(v, str) and v:
            haystacks.append(v)
    # Tag matches: each tag string standalone; explicit gh: form first
    for t in (item.get("tags") or []):
        if not isinstance(t, str):
            continue
        m = _GH_TAG_RE.match(t)
        if m:
            return m.group(1), m.group(2), int(m.group(3))
        haystacks.append(t)

    text = "\n".join(haystacks)
    if not text:
        return None

    # Long URL takes precedence
    m = _GH_URL_RE.search(text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = _GH_SHORT_RE.search(text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def _gh_fetch_issue_state(owner: str, repo: str, number: int, _runner=None) -> Dict[str, Any]:
    """Query the GitHub API for issue/PR state. Returns:
        {ok: bool, state: str, merged: bool|None, closed_at: str|None,
         merge_commit_sha: str|None, state_reason: str|None}
    or {ok: False, error: str} on failure.

    `_runner` is for test injection; defaults to subprocess.run.
    """
    if _runner is None:
        _runner = subprocess.run

    try:
        result = _runner(
            ["gh", "api", f"/repos/{owner}/{repo}/issues/{number}",
             "--jq", '{state, closed_at, state_reason, pull_request: (.pull_request != null)}'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout)[:200]}
        meta = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)[:200]}

    out = {
        "ok": True,
        "state": meta.get("state"),
        "closed_at": meta.get("closed_at"),
        "state_reason": meta.get("state_reason"),
        "merged": None,
        "merge_commit_sha": None,
    }

    # If it's a PR, fetch the merge bit separately. We skip this on
    # non-PR issues to save an API call.
    if meta.get("pull_request"):
        try:
            pr_result = _runner(
                ["gh", "api", f"/repos/{owner}/{repo}/pulls/{number}",
                 "--jq", '{merged, merge_commit_sha}'],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if pr_result.returncode == 0:
                pr_meta = json.loads(pr_result.stdout)
                out["merged"] = bool(pr_meta.get("merged"))
                out["merge_commit_sha"] = pr_meta.get("merge_commit_sha")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
    return out


def _resolve_action_for_external(state_meta: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Map GitHub issue/PR state -> (action, note) for bulk_action.

    Returns None when the LED should be left alone (still open / fetch error).

    Per LED-1146 deliberation:
      - PR merged → mark_done with merge SHA
      - issue/PR closed with state_reason='completed' → mark_done with closed_at
      - issue/PR closed with state_reason='not_planned' (or no reason) → archive
      - state='open' → None (leave alone)
      - fetch error → None (leave alone)
    """
    if not state_meta.get("ok"):
        return None
    if state_meta.get("state") != "closed":
        return None

    # PR with merged=True is unambiguous "we shipped this"
    if state_meta.get("merged"):
        sha = (state_meta.get("merge_commit_sha") or "")[:8]
        return {"action": "mark_done", "note": f"AUTO-CLOSE: merged {sha}".rstrip()}

    state_reason = state_meta.get("state_reason")
    closed_at = (state_meta.get("closed_at") or "")[:19]

    if state_reason == "completed":
        return {"action": "mark_done", "note": f"AUTO-CLOSE: closed-completed {closed_at}".rstrip()}

    # not_planned, duplicate, bot triage, no-reason → archive
    return {"action": "archive", "note": f"AUTO-CLOSE: closed-not-planned {closed_at}".rstrip()}


def auto_close_linked_external(
    project_path: str = ".",
    dry_run: bool = True,
    max_items: int = 200,
    _gh_runner=None,
) -> Dict[str, Any]:
    """Walk open ledger items, detect linked GitHub issues/PRs, and propose
    closing any whose external counterpart already resolved.

    LED-1145 Phase 2 #1. Built on top of bulk_action() from PR-B (uses its
    `mark_done` and `archive` actions).

    Args:
        project_path: ledger root.
        dry_run: True (default) returns a plan without writing.
        max_items: hard cap on the number of items processed in one call.
            When the candidate set exceeds this, we process the first N and
            mark `truncated=True` in the response.
        _gh_runner: test-only hook for stubbing the gh CLI.

    Returns:
        {
            "dry_run": bool,
            "scanned": int,             # items walked
            "linked": int,              # items with a recognised github reference
            "would_close" or "closed": [
                {"id", "external": "owner/repo#num", "action", "note", "state"},
                ...
            ],
            "left_open": [{"id", "external", "reason"}],   # external still open / fetch error
            "errors": [{"id", "reason"}],
            "truncated": bool,
            "summary": {...},
        }
    """
    _ensure(project_path)
    listing = list_items(status__in=["open", "in_progress", "blocked"], limit=10_000, project_path=project_path)

    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    truncated = False
    if len(candidates) > max_items:
        candidates = candidates[:max_items]
        truncated = True

    # Per-call cache so the same external URL referenced by multiple LEDs
    # only triggers one gh API call.
    fetch_cache: Dict[tuple, Dict[str, Any]] = {}

    would_close: List[Dict[str, Any]] = []
    left_open: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    scanned = 0
    linked = 0

    for item in candidates:
        scanned += 1
        ext = _extract_external_link(item)
        if ext is None:
            continue
        linked += 1
        owner, repo, number = ext
        external_label = f"{owner}/{repo}#{number}"

        cache_key = (owner.lower(), repo.lower(), number)
        if cache_key not in fetch_cache:
            fetch_cache[cache_key] = _gh_fetch_issue_state(owner, repo, number, _runner=_gh_runner)
        state_meta = fetch_cache[cache_key]

        if not state_meta.get("ok"):
            errors.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": f"gh_api_failed: {state_meta.get('error', 'unknown')}",
            })
            continue

        if state_meta.get("state") != "closed":
            left_open.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": "external_still_open",
            })
            continue

        action_plan = _resolve_action_for_external(state_meta)
        if action_plan is None:
            left_open.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": "no_action_resolved",
            })
            continue

        would_close.append({
            "id": item.get("id"),
            "external": external_label,
            "action": action_plan["action"],
            "note": action_plan["note"],
            "state": state_meta.get("state"),
            "merged": state_meta.get("merged"),
        })

    if dry_run:
        return {
            "dry_run": True,
            "scanned": scanned,
            "linked": linked,
            "would_close": would_close,
            "left_open": left_open,
            "errors": errors,
            "truncated": truncated,
            "max_items": max_items,
            "summary": {
                "scanned": scanned,
                "linked": linked,
                "would_close": len(would_close),
                "left_open": len(left_open),
                "errors": len(errors),
                "truncated": truncated,
            },
        }

    # Apply mode: dispatch each plan through bulk_action one item at a time
    # (so per-item action and note can vary).
    closed_results: List[Dict[str, Any]] = []
    for plan in would_close:
        ba = bulk_action(
            item_ids=[plan["id"]],
            action=plan["action"],
            dry_run=False,
            note=plan["note"],
            project_path=project_path,
        )
        if ba.get("summary", {}).get("changed"):
            closed_results.append(plan)
        else:
            errors.append({
                "id": plan["id"],
                "reason": f"bulk_action_failed: {ba.get('errors') or ba.get('error') or 'unknown'}",
            })

    return {
        "dry_run": False,
        "scanned": scanned,
        "linked": linked,
        "closed": closed_results,
        "left_open": left_open,
        "errors": errors,
        "truncated": truncated,
        "max_items": max_items,
        "summary": {
            "scanned": scanned,
            "linked": linked,
            "closed": len(closed_results),
            "left_open": len(left_open),
            "errors": len(errors),
            "truncated": truncated,
        },
    }


# ── LED-1145 Phase 2 #2: proposal-first grooming ─────────────────────────

import datetime as _dt
from collections import defaultdict as _defaultdict


def _is_garbage_venture(name: str) -> bool:
    """Detect test/scratch venture buckets that should be archived in bulk.

    Pattern matches: tmp*, test_*, venture_<single-letter>, custom-venture.
    Deliberately does NOT match "unknown" — those are orphaned items that
    warrant a separate review pass, not auto-archive.
    """
    if not name:
        return False
    if name.startswith(("tmp", "test_")):
        return True
    if name == "custom-venture":
        return True
    # venture_a / venture_b / venture_z (test fixture pattern)
    if _re.match(r"^venture_[a-z]$", name):
        return True
    return False


def _title_prefix(title: str, length: int = 50) -> str:
    """Normalise a title for fuzzy duplicate detection.

    Strips bracketed prefixes like '[DELIMIT]' and lowercases so
    "[DELIMIT] GitHub outreach: foo" and "GitHub outreach: foo" group.
    """
    if not title:
        return ""
    cleaned = _re.sub(r"^\s*\[[^\]]+\]\s*", "", title)
    return cleaned.strip().lower()[:length]


def _parse_iso(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 timestamp. Tolerates the trailing 'Z' suffix and
    naive timestamps. Returns None for unparseable input."""
    if not ts:
        return None
    try:
        # 2026-04-13T08:00:00Z and 2026-04-13T08:00:00 both supported
        v = ts.rstrip("Z")
        # Strip fractional seconds if present
        if "." in v:
            v = v.split(".", 1)[0]
        return _dt.datetime.fromisoformat(v).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def groom_proposal(
    project_path: str = ".",
    stale_days: int = 30,
    dup_min_count: int = 3,
    max_per_category: int = 50,
) -> Dict[str, Any]:
    """Read-only grooming proposal: surfaces stale / duplicate / garbage-venture
    items as a structured plan for the founder to review and apply.

    LED-1145 Phase 2 #2. The deliberation explicitly said "risky operations
    like deduplication and mass-cancellation must not be a single atomic
    action. The AI proposes a plan; the founder approves; execution goes
    through the safe bulk_action tool." This function is the proposal half.

    Categories detected:
      - stale_open: status open|in_progress|blocked AND updated_at older
        than `stale_days`. Suggested action: archive.
      - duplicate_titles: groups of >= `dup_min_count` items sharing the
        same normalised title prefix (50 chars, brackets stripped). Suggested
        action: archive (de-duped). The first item in the group is kept.
      - garbage_venture: items whose venture matches the test/scratch
        pattern (tmp*, test*, venture_<letter>, custom-venture, unknown).
        Suggested action: archive.

    Out of scope (separate detectors / future PRs):
      - linked-external auto-close — already shipped as a separate tool
        (delimit_ledger_auto_close_external)
      - P0 inflation review — surfaced separately via list_items
      - cross-venture orphan cleanup — needs portfolio policy

    Args:
        project_path: ledger root.
        stale_days: threshold for "stale_open" detector. Default 30.
        dup_min_count: minimum group size for "duplicate_titles". Default 3.
        max_per_category: cap per category in the response. Default 50.

    Returns:
        {
            "proposals": [
                {
                    "category": str,
                    "rationale": str,
                    "items": [{"id", "title", "venture", "status", "updated_at"}, ...],
                    "suggested_action": str,
                    "ready_to_apply": str,  # copy-pasteable bulk_action invocation hint
                    "truncated": bool,
                    "total_in_category": int,
                },
                ...
            ],
            "summary": {
                "total_categories": int,
                "total_items": int,
                "stale_open": int,
                "duplicate_titles": int,
                "garbage_venture": int,
            }
        }
    """
    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    now = _dt.datetime.now(_dt.timezone.utc)
    stale_threshold = now - _dt.timedelta(days=stale_days)

    stale_items: List[Dict[str, Any]] = []
    garbage_items: List[Dict[str, Any]] = []
    title_groups: Dict[str, List[Dict[str, Any]]] = _defaultdict(list)

    for item in candidates:
        # garbage_venture (highest precedence — short-circuits other categories)
        if _is_garbage_venture(item.get("venture", "") or ""):
            garbage_items.append(item)
            continue

        # stale_open
        ts = item.get("updated_at") or item.get("created_at")
        parsed = _parse_iso(ts)
        if parsed and parsed < stale_threshold:
            stale_items.append(item)

        # duplicate_titles (always grouped — overlapping with stale is OK,
        # the apply-side de-dupe relies on the founder picking one category)
        prefix = _title_prefix(item.get("title", ""))
        if prefix:
            title_groups[prefix].append(item)

    duplicate_groups = {
        prefix: items for prefix, items in title_groups.items() if len(items) >= dup_min_count
    }

    def _trim(items):
        total = len(items)
        truncated = total > max_per_category
        return items[:max_per_category], total, truncated

    proposals = []

    if stale_items:
        sliced, total, truncated = _trim(stale_items)
        proposals.append({
            "category": "stale_open",
            "rationale": f"{total} items have been open for >{stale_days} days with no update; "
                         f"either the work needs revival or the LED was set-and-forget debt.",
            "items": [
                {
                    "id": i.get("id"),
                    "title": (i.get("title") or "")[:80],
                    "venture": i.get("venture"),
                    "status": i.get("status"),
                    "updated_at": i.get("updated_at") or i.get("created_at"),
                }
                for i in sliced
            ],
            "suggested_action": "archive",
            "ready_to_apply": (
                f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in sliced)}', "
                f"action='archive', dry_run=True)"
            ),
            "truncated": truncated,
            "total_in_category": total,
        })

    if duplicate_groups:
        # One proposal entry per group so the founder can decide group-by-group
        for prefix, group in sorted(duplicate_groups.items()):
            sliced, total, truncated = _trim(group)
            # Keep the most-recent (or first) item, archive the rest
            sliced_sorted = sorted(
                sliced,
                key=lambda x: x.get("updated_at") or x.get("created_at") or "",
                reverse=True,
            )
            keep = sliced_sorted[0]
            archive = sliced_sorted[1:]
            proposals.append({
                "category": "duplicate_titles",
                "rationale": (
                    f"{total} items share the title prefix {prefix!r}. "
                    f"Suggesting we keep {keep.get('id')} (most recent) "
                    f"and archive the other {len(archive)}."
                ),
                "items": [
                    {
                        "id": i.get("id"),
                        "title": (i.get("title") or "")[:80],
                        "venture": i.get("venture"),
                        "status": i.get("status"),
                        "updated_at": i.get("updated_at") or i.get("created_at"),
                        "_role": "keep" if i.get("id") == keep.get("id") else "archive",
                    }
                    for i in sliced_sorted
                ],
                "suggested_action": "archive",
                "ready_to_apply": (
                    f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in archive)}', "
                    f"action='archive', dry_run=True)"
                ),
                "truncated": truncated,
                "total_in_category": total,
            })

    if garbage_items:
        sliced, total, truncated = _trim(garbage_items)
        proposals.append({
            "category": "garbage_venture",
            "rationale": (
                f"{total} items belong to test/scratch venture buckets "
                f"(tmp*, test*, venture_<letter>, custom-venture, unknown) that "
                f"shouldn't be in production data. Safe to archive in bulk."
            ),
            "items": [
                {
                    "id": i.get("id"),
                    "title": (i.get("title") or "")[:80],
                    "venture": i.get("venture"),
                    "status": i.get("status"),
                    "updated_at": i.get("updated_at") or i.get("created_at"),
                }
                for i in sliced
            ],
            "suggested_action": "archive",
            "ready_to_apply": (
                f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in sliced)}', "
                f"action='archive', dry_run=True)"
            ),
            "truncated": truncated,
            "total_in_category": total,
        })

    summary = {
        "total_categories": len(proposals),
        "total_items": sum(p["total_in_category"] for p in proposals),
        "stale_open": len(stale_items),
        "duplicate_titles": sum(len(g) for g in duplicate_groups.values()),
        "garbage_venture": len(garbage_items),
    }

    return {
        "proposals": proposals,
        "summary": summary,
    }


# ── LED-1145 Phase 2 #4: stale-TTL auto-cancel ────────────────────────────

# After this many days of dormancy, an open item is considered stale enough
# that auto-archive is the safe default. groom_proposal still flags items
# at 30d for triage; this stricter cap is for items that crossed the
# triage threshold but never got reviewed. Override via env or arg.
STALE_TTL_DEFAULT_DAYS = 60


def _stale_ttl_default() -> int:
    """Resolve the active stale-TTL threshold from env. 0 disables auto-cancel."""
    raw = os.environ.get("DELIMIT_STALE_TTL_DAYS", "")
    if raw == "":
        return STALE_TTL_DEFAULT_DAYS
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return STALE_TTL_DEFAULT_DAYS


def auto_cancel_stale(
    project_path: str = ".",
    threshold_days: Optional[int] = None,
    dry_run: bool = True,
    max_items: int = 200,
) -> Dict[str, Any]:
    """Auto-archive open items that have been dormant past the stale TTL.

    LED-1145 Phase 2 #4. Composes Phase 2 #2's stale-detection logic with
    Phase 1 PR-B's bulk_action(action="archive"). Same dry_run-default
    pattern as auto_close_linked_external — caller passes dry_run=False
    explicitly to apply.

    Distinct from groom_proposal's stale_open category because:
      - The threshold is stricter (default 60d vs groom's 30d)
      - It auto-applies on dry_run=False (groom is purely propose)
      - It's intended for nightly automation; groom is for interactive use

    Args:
        project_path: ledger root.
        threshold_days: dormancy threshold. Default reads
            DELIMIT_STALE_TTL_DAYS env (60 if unset). 0 disables.
        dry_run: True (default) returns a plan; False applies via bulk_action.
        max_items: cap per call. Items beyond the cap surface in
            `truncated=True` with `total_candidates` set.

    Returns:
        {
            "dry_run": bool,
            "threshold_days": int,
            "would_cancel" or "cancelled": [{id, title, venture, status, last_seen}, ...],
            "errors": [...],
            "summary": {"scanned": int, "stale": int, "cancelled": int, "errors": int, "truncated": bool},
        }
    """
    if threshold_days is None:
        threshold_days = _stale_ttl_default()
    if threshold_days == 0:
        return {
            "dry_run": dry_run,
            "threshold_days": 0,
            "would_cancel" if dry_run else "cancelled": [],
            "errors": [],
            "summary": {
                "scanned": 0, "stale": 0, "cancelled": 0,
                "errors": 0, "truncated": False,
                "note": "DELIMIT_STALE_TTL_DAYS=0 disables auto-cancel",
            },
        }

    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    now = _dt.datetime.now(_dt.timezone.utc)
    stale_threshold = now - _dt.timedelta(days=threshold_days)

    stale_items: List[Dict[str, Any]] = []
    for item in candidates:
        ts = item.get("updated_at") or item.get("created_at")
        parsed = _parse_iso(ts)
        if parsed and parsed < stale_threshold:
            stale_items.append(item)

    total_candidates = len(stale_items)
    truncated = total_candidates > max_items
    stale_items = stale_items[:max_items]

    summary_records = [
        {
            "id": i.get("id"),
            "title": (i.get("title") or "")[:80],
            "venture": i.get("venture"),
            "status": i.get("status"),
            "last_seen": i.get("updated_at") or i.get("created_at"),
        }
        for i in stale_items
    ]

    if dry_run:
        return {
            "dry_run": True,
            "threshold_days": threshold_days,
            "would_cancel": summary_records,
            "errors": [],
            "summary": {
                "scanned": len(candidates),
                "stale": total_candidates,
                "would_cancel": len(stale_items),
                "errors": 0,
                "truncated": truncated,
            },
        }

    # Apply via bulk_action(action="archive") so the same audit / no-hard-
    # delete invariants used everywhere else apply here too.
    if not stale_items:
        return {
            "dry_run": False,
            "threshold_days": threshold_days,
            "cancelled": [],
            "errors": [],
            "summary": {
                "scanned": len(candidates), "stale": 0,
                "cancelled": 0, "errors": 0, "truncated": False,
            },
        }

    ids = [i.get("id") for i in stale_items if i.get("id")]
    bulk_result = bulk_action(
        item_ids=ids,
        action="archive",
        dry_run=False,
        note=f"auto-cancel: dormant > {threshold_days} days",
        project_path=project_path,
    )

    return {
        "dry_run": False,
        "threshold_days": threshold_days,
        "cancelled": summary_records,
        "errors": bulk_result.get("errors", []),
        "summary": {
            "scanned": len(candidates),
            "stale": total_candidates,
            "cancelled": bulk_result.get("summary", {}).get("changed", 0),
            "errors": len(bulk_result.get("errors", [])),
            "truncated": truncated,
        },
    }


# ── LED-1145 Phase 2 capstone: ledger health summary ─────────────────────

# Traffic-light thresholds for each category. Tuned from observed
# real-world ledger sizes — ~50 stale items is when the "groom me"
# signal becomes loud; ~20 is yellow heads-up.
_HEALTH_STALE_RED = 50
_HEALTH_STALE_YELLOW = 20
_HEALTH_GARBAGE_RED = 10
_HEALTH_GARBAGE_YELLOW = 1
_HEALTH_DUPE_RED = 5
_HEALTH_DUPE_YELLOW = 2


def _grade(value: int, yellow: int, red: int) -> str:
    if value >= red:
        return "red"
    if value >= yellow:
        return "yellow"
    return "green"


def _worst_grade(*grades: str) -> str:
    """Worst-of N traffic lights: red > yellow > green."""
    if "red" in grades:
        return "red"
    if "yellow" in grades:
        return "yellow"
    return "green"


def health_summary(
    project_path: str = ".",
    stale_days: int = 30,
    dup_min_count: int = 3,
) -> Dict[str, Any]:
    """One-shot health check for the ledger. Composes list_items +
    groom_proposal + the P0 quota helper into a single traffic-light
    verdict and a list of concrete next actions.

    LED-1145 capstone — closes the loop on the entire ledger-tooling
    refactor. Designed for nightly/weekly review or for the founder's
    session-start status snapshot.

    Args:
        project_path: ledger root.
        stale_days: threshold passed through to groom_proposal.
        dup_min_count: threshold passed through to groom_proposal.

    Returns:
        {
            "venture": str,
            "totals": {"unresolved": int, "open": int, "in_progress": int, "blocked": int, ...},
            "p0":         {"count": int, "quota": int, "health": str},
            "stale":      {"count": int, "health": str},
            "duplicates": {"groups": int, "items": int, "health": str},
            "garbage":    {"count": int, "health": str},
            "overall_health": "green" | "yellow" | "red",
            "next_actions": [
                {"reason": str, "tool": str, "args": dict},
                ...
            ],
        }
    """
    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    summary = listing.get("summary", {})
    venture = listing.get("venture", "unknown")

    p0_count = _count_unresolved_p0(project_path=project_path)
    p0_quota = _p0_soft_quota()
    p0_health = "green"
    if p0_quota > 0:
        if p0_count > p0_quota * 1.5:
            p0_health = "red"
        elif p0_count > p0_quota:
            p0_health = "yellow"

    proposal = groom_proposal(
        project_path=project_path,
        stale_days=stale_days,
        dup_min_count=dup_min_count,
        max_per_category=10_000,
    )
    proposal_summary = proposal.get("summary", {})
    stale_count = proposal_summary.get("stale_open", 0)
    dup_items = proposal_summary.get("duplicate_titles", 0)
    garbage_count = proposal_summary.get("garbage_venture", 0)
    dup_groups = sum(
        1 for p in proposal.get("proposals", [])
        if p.get("category") == "duplicate_titles"
    )

    stale_health = _grade(stale_count, _HEALTH_STALE_YELLOW, _HEALTH_STALE_RED)
    dup_health = _grade(dup_groups, _HEALTH_DUPE_YELLOW, _HEALTH_DUPE_RED)
    garbage_health = _grade(garbage_count, _HEALTH_GARBAGE_YELLOW, _HEALTH_GARBAGE_RED)

    overall = _worst_grade(p0_health, stale_health, dup_health, garbage_health)

    # Build next-actions list ordered by impact
    next_actions: List[Dict[str, Any]] = []
    if garbage_count > 0:
        next_actions.append({
            "reason": f"{garbage_count} item(s) in test/scratch venture buckets",
            "tool": "delimit_ledger_groom",
            "args": {"venture": venture},
            "follow_up": "Apply the garbage_venture proposal via delimit_ledger_bulk(action='archive')",
        })
    if dup_groups > 0:
        next_actions.append({
            "reason": f"{dup_groups} duplicate-title group(s) covering {dup_items} items",
            "tool": "delimit_ledger_groom",
            "args": {"venture": venture, "dup_min_count": dup_min_count},
            "follow_up": "Review duplicate_titles proposals; archive all but most-recent in each group",
        })
    if stale_count > 0:
        next_actions.append({
            "reason": f"{stale_count} item(s) dormant >{stale_days}d",
            "tool": "delimit_ledger_auto_cancel_stale",
            "args": {"venture": venture, "threshold_days": 60, "dry_run": True},
            "follow_up": "Run with dry_run=False after reviewing the plan",
        })
    if p0_quota > 0 and p0_count > p0_quota:
        next_actions.append({
            "reason": f"P0 inflation: {p0_count} unresolved P0s (threshold {p0_quota})",
            "tool": "delimit_ledger_list",
            "args": {"venture": venture, "priority_in": "P0", "status_in": "open,in_progress,blocked", "fields": "slim"},
            "follow_up": "Triage each: ship, demote to P1, or archive",
        })
    if not next_actions:
        next_actions.append({
            "reason": "All categories green",
            "tool": None,
            "args": {},
            "follow_up": "No grooming required",
        })

    return {
        "venture": venture,
        "totals": {
            "unresolved": summary.get("total", 0),
            "open": summary.get("open", 0),
            "in_progress": summary.get("in_progress", 0),
            "blocked": summary.get("blocked", 0),
        },
        "p0": {"count": p0_count, "quota": p0_quota, "health": p0_health},
        "stale": {"count": stale_count, "health": stale_health, "threshold_days": stale_days},
        "duplicates": {"groups": dup_groups, "items": dup_items, "health": dup_health},
        "garbage": {"count": garbage_count, "health": garbage_health},
        "overall_health": overall,
        "next_actions": next_actions,
    }
