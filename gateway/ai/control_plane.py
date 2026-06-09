"""Unified control-plane queue aggregator (LED-1709, Phase 0).

READ-ONLY pure data layer. This module is the single shared backend that
BOTH the CLI (direct MCP via ``delimit_control``) and the web dashboard
(delimit-ui ``app/api/mcp`` route, MCP-over-HTTP) render. It therefore has
NO CLI- or web-specific coupling: it only reads on-disk ``~/.delimit`` stores
and returns plain dicts.

It aggregates four existing storage sources into ONE normalized queue:

  1. Attestations  -> class="attestation"  (the moat object; render first-class)
  2. Approvals     -> class="approval"     (founder directives awaiting ack)
  3. Ledger        -> class="sensing" (STR-*) | "ops" (LED-*/others)
  4. Dispatches    -> class="ops"           (work-orders / agent actions)

Every item is normalized to the SAME shape:

    {
        "id": str,
        "class": "attestation" | "approval" | "sensing" | "ops",
        "state": str,           # pending|in_progress|approved|rejected|done|open|...
        "title": str,
        "source": str,          # which store produced it
        "created": str,         # ISO-8601
        "summary": str,         # <= 200 chars
        "links": dict,          # optional: replay_url, ledger_id, filepath, ...
    }

Design rules (Phase 0):
  * Read-only. NEVER writes to any ~/.delimit store.
  * Resilient/best-effort: a malformed or missing source must not crash the
    whole queue. Each source is wrapped in try/except; bad records are
    skipped, not fatal.
  * Path resolution honors DELIMIT_HOME / DELIMIT_NAMESPACE_ROOT (LED-1188),
    falling back to ~/.delimit. No hardcoded /home/delimit paths.
  * stdlib only (plus optional reuse of ai.inbox_daemon's directive reader).

The aggregator keeps the attestation lane retrievable distinctly so a client
can show attestations separately from the (potentially ~1170) sensing items.
See ``build_queue`` for the balancing behavior when class_filter is empty.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Path resolution (LED-1188 / LED-1715 conventions) ────────────────────

def _delimit_home() -> Path:
    """Resolve the ~/.delimit home, honoring DELIMIT_HOME / DELIMIT_NAMESPACE_ROOT.

    Read dynamically (not module-level) so tests can point DELIMIT_HOME at a
    tmp dir per-test without import-order issues.
    """
    for env_key in ("DELIMIT_HOME", "DELIMIT_NAMESPACE_ROOT"):
        val = os.environ.get(env_key)
        if val:
            return Path(val)
    return Path.home() / ".delimit"


_VALID_CLASSES = {"attestation", "approval", "sensing", "ops"}


# ── Helpers ──────────────────────────────────────────────────────────────

def _truncate(text: Any, limit: int = 200) -> str:
    """Coerce to str and cap at `limit` chars (summary contract)."""
    if text is None:
        return ""
    s = str(text).replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s


def _iter_jsonl(path: Path):
    """Yield parsed records from a JSONL file, skipping malformed lines.

    Best-effort: a missing file yields nothing; a malformed line is skipped.
    """
    try:
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def _sort_key(item: Dict[str, Any]) -> str:
    """Sort key for newest-first ordering. Empty `created` sorts last."""
    return item.get("created") or ""


# ── Source 1: Attestations (the moat object) ─────────────────────────────

def _load_attestations() -> List[Dict[str, Any]]:
    """Normalize ~/.delimit/attestations/att_*.json into queue items.

    Real schema (delimit.attestation.v1):
        id, signature, signature_alg, bundle{schema, kind, wrapped_command,
        repo_root, before_head, after_head, started_at, completed_at,
        wrapped_exit, changed_files, governance{gates, violations, advisory}}

    Mapping:
        id      <- top-level "id"
        state   <- derived from governance: rejected (violations & not advisory),
                   done (clean), advisory (advisory-only gate)
        title   <- bundle.kind or wrapped_command
        created <- bundle.completed_at or started_at
        summary <- wrapped_command + verdict
        links   <- {replay_url?, repo, signature}
    """
    items: List[Dict[str, Any]] = []
    att_dir = _delimit_home() / "attestations"
    try:
        if not att_dir.is_dir():
            return items
        files = sorted(att_dir.glob("att_*.json"))
    except OSError:
        return items

    for fp in files:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        try:
            att_id = str(raw.get("id") or fp.stem)
            bundle = raw.get("bundle") or {}
            gov = bundle.get("governance") or {}
            violations = gov.get("violations") or []
            advisory = bool(gov.get("advisory"))

            if violations and not advisory:
                state = "rejected"
            elif advisory:
                state = "advisory"
            else:
                state = "done"

            kind = bundle.get("kind") or bundle.get("schema") or "attestation"
            wrapped = bundle.get("wrapped_command") or ""
            created = (
                bundle.get("completed_at")
                or bundle.get("started_at")
                or _file_mtime_iso(fp)
            )

            verdict = "clean" if not violations else f"{len(violations)} violation(s)"
            summary = _truncate(
                f"{wrapped or kind} — {verdict}"
                + (f"; {', '.join(str(v) for v in violations)}" if violations else "")
            )

            links: Dict[str, Any] = {}
            # Replay URL is not stored in the att_*.json today, but probe a few
            # plausible field names so the lane is forward-compatible.
            for key in ("replay_url", "replay", "url"):
                if bundle.get(key):
                    links["replay_url"] = bundle[key]
                    break
                if raw.get(key):
                    links["replay_url"] = raw[key]
                    break
            if bundle.get("repo_root"):
                links["repo"] = bundle["repo_root"]
            if raw.get("signature"):
                links["signature"] = raw["signature"]

            items.append(
                {
                    "id": att_id,
                    "class": "attestation",
                    "state": state,
                    "title": _truncate(str(kind), 200),
                    "source": "attestations",
                    "created": _to_iso(created),
                    "summary": summary,
                    "links": links,
                    "_raw": raw,
                }
            )
        except Exception:
            # Per-record resilience: skip a bad record, keep the queue alive.
            continue
    return items


# ── Source 2: Approvals (founder directives awaiting ack) ────────────────

def _load_approvals() -> List[Dict[str, Any]]:
    """Normalize founder directives awaiting completion into queue items.

    Source: ~/.delimit/inbox_routing.jsonl, where the inbox daemon logs
    `founder_directive_received` and `founder_directive_completed` events.
    A directive is "pending approval" until a matching completed event by
    subject exists.

    We deliberately do NOT delegate to ai.inbox_daemon.get_pending_directives:
    that reader binds ROUTING_LOG to Path.home()/".delimit" at import time and
    ignores DELIMIT_HOME, which would make the control plane untestable and
    break customer installs that relocate the home. _local_pending_directives()
    applies the identical dedup-by-subject logic but resolves the path through
    _delimit_home() each call.

    Mapping:
        id      <- "DIR-" + msg_id / fingerprint / subject-hash
        state   <- "awaiting_approval"
        title   <- subject
        created <- timestamp
        summary <- from / classification
        links   <- {thread_url?, msg_id?}
    """
    items: List[Dict[str, Any]] = []
    directives = _local_pending_directives()

    for d in directives:
        try:
            subject = d.get("subject") or d.get("directive_subject") or ""
            msg_id = d.get("msg_id") or d.get("fingerprint") or ""
            ident = msg_id or _stable_hash(subject)
            created = d.get("timestamp") or d.get("date") or ""
            sender = d.get("from") or d.get("sender") or ""
            classification = d.get("classification") or "owner-action"

            links: Dict[str, Any] = {}
            if d.get("thread_url"):
                links["thread_url"] = d["thread_url"]
            if msg_id:
                links["msg_id"] = msg_id

            items.append(
                {
                    "id": f"DIR-{ident}",
                    "class": "approval",
                    "state": "awaiting_approval",
                    "title": _truncate(subject or "(no subject)", 200),
                    "source": "inbox_routing",
                    "created": _to_iso(created),
                    "summary": _truncate(f"from {sender} [{classification}]"),
                    "links": links,
                    "_raw": d,
                }
            )
        except Exception:
            continue
    return items


def _local_pending_directives() -> List[Dict[str, Any]]:
    """Fallback parse of pending founder directives from inbox_routing.jsonl.

    Mirrors ai.inbox_daemon.get_pending_directives(): a directive is pending
    unless a `founder_directive_completed` event shares its subject.
    """
    routing = _delimit_home() / "inbox_routing.jsonl"
    received: List[Dict[str, Any]] = []
    completed: set = set()
    for entry in _iter_jsonl(routing):
        try:
            ev = entry.get("event")
            if ev == "founder_directive_completed":
                completed.add(entry.get("directive_subject", ""))
            elif ev == "founder_directive_received":
                received.append(entry)
        except (AttributeError, KeyError):
            continue
    return [r for r in received if r.get("subject", "") not in completed]


# ── Source 3: Ledger (open/pending items) ────────────────────────────────

def _load_ledger() -> List[Dict[str, Any]]:
    """Normalize open/pending ledger items into queue items.

    Walks the central ~/.delimit/ledger/*.jsonl AND the partitioned
    ~/.delimit/ledger-v2/<venture>/*.jsonl trees directly (read-only,
    cross-venture). We do NOT route through ledger_manager.list_items
    because that applies venture auto-detection + cursor pagination tuned
    for a single-project call; the control plane wants every venture at once.

    Record schema (observed): id, title, status, priority, description,
    venture, tags, created_at, updated_at, hash, type.

    Class split (per LED-1709 spec):
        STR-* ids        -> class="sensing"
        LED-*/all others -> class="ops"

    Only open/pending-ish items are surfaced; done/closed/rejected are
    excluded from the active queue (a closed ledger item is not "in the
    queue"). De-duplicated by id (last write wins by created/updated time).
    """
    home = _delimit_home()
    candidates: List[Path] = []
    for base in (home / "ledger", home / "ledger-v2"):
        try:
            if base.is_dir():
                candidates.extend(sorted(base.rglob("*.jsonl")))
        except OSError:
            continue

    # Active states only. Anything not clearly terminal is treated as active.
    terminal = {"done", "closed", "complete", "completed", "rejected", "cancelled",
                "canceled", "resolved", "shipped", "merged"}

    by_id: Dict[str, Dict[str, Any]] = {}
    for path in candidates:
        # Skip non-item ledger files (links/updates are not queue items).
        if path.name in {"links.jsonl", "updates.jsonl"}:
            continue
        for rec in _iter_jsonl(path):
            try:
                item_id = rec.get("id")
                if not item_id:
                    continue
                item_id = str(item_id)
                status = str(rec.get("status") or "open").lower()
                if status in terminal:
                    continue

                cls = "sensing" if item_id.upper().startswith("STR-") else "ops"
                created = rec.get("created_at") or rec.get("created") or ""
                updated = rec.get("updated_at") or created

                links: Dict[str, Any] = {"ledger_id": item_id}
                if rec.get("venture"):
                    links["venture"] = rec["venture"]

                norm = {
                    "id": item_id,
                    "class": cls,
                    "state": status,
                    "title": _truncate(rec.get("title") or item_id, 200),
                    "source": _ledger_source_label(path, home),
                    "created": _to_iso(created),
                    "summary": _truncate(rec.get("description") or rec.get("title") or ""),
                    "links": links,
                    "_raw": rec,
                    "_updated": _to_iso(updated),
                }
                prev = by_id.get(item_id)
                if prev is None or norm["_updated"] >= prev.get("_updated", ""):
                    by_id[item_id] = norm
            except Exception:
                continue

    out: List[Dict[str, Any]] = []
    for it in by_id.values():
        it.pop("_updated", None)
        out.append(it)
    return out


def _ledger_source_label(path: Path, home: Path) -> str:
    """Compact source label, e.g. 'ledger-v2/wire-report/strategy'."""
    try:
        rel = path.relative_to(home)
        return str(rel.with_suffix(""))
    except ValueError:
        return path.stem


# ── Source 4: Dispatches (work-orders / agent actions) ───────────────────

def _load_dispatches() -> List[Dict[str, Any]]:
    """Normalize work-orders and agent-action records into queue items.

    Sources:
        ~/.delimit/work-orders/*.json   (WO-*.json — primary)
        ~/.delimit/agent_actions/*.json (agent dispatch audit records)

    Work-order schema (observed): id, title, goal, status, priority,
    created_at, ledger_item_id, filepath, estimated_minutes.

    Mapping -> class="ops":
        id      <- "id"
        state   <- "status"
        title   <- "title"
        created <- "created_at"
        summary <- "goal"
        links   <- {filepath?, ledger_id?}
    """
    items: List[Dict[str, Any]] = []
    home = _delimit_home()

    # Active states only; completed work-orders aren't in the active queue.
    terminal = {"done", "completed", "complete", "closed", "cancelled", "canceled"}

    for sub, source in (("work-orders", "work-orders"), ("agent_actions", "agent_actions")):
        d = home / sub
        try:
            if not d.is_dir():
                continue
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for fp in files:
            try:
                raw = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            # agent_actions may store either a single record or a list.
            records = raw if isinstance(raw, list) else [raw]
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                try:
                    item_id = str(rec.get("id") or rec.get("action_id") or fp.stem)
                    status = str(rec.get("status") or "pending").lower()
                    if status in terminal:
                        continue
                    created = rec.get("created_at") or rec.get("created") or rec.get("timestamp") or ""
                    title = rec.get("title") or rec.get("goal") or rec.get("task") or item_id

                    links: Dict[str, Any] = {}
                    if rec.get("filepath"):
                        links["filepath"] = rec["filepath"]
                    if rec.get("ledger_item_id"):
                        links["ledger_id"] = rec["ledger_item_id"]

                    items.append(
                        {
                            "id": item_id,
                            "class": "ops",
                            "state": status,
                            "title": _truncate(title, 200),
                            "source": source,
                            "created": _to_iso(created),
                            "summary": _truncate(rec.get("goal") or rec.get("context") or title),
                            "links": links,
                            "_raw": rec,
                        }
                    )
                except Exception:
                    continue
    return items


# ── ISO / hashing utilities ──────────────────────────────────────────────

def _to_iso(value: Any) -> str:
    """Best-effort coercion to an ISO-8601 string; empty string on failure."""
    if not value:
        return ""
    s = str(value)
    return s


def _file_mtime_iso(fp: Path) -> str:
    try:
        return datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return ""


def _stable_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


# ── Public API ─────────────────────────────────────────────────────────--

def _all_items() -> List[Dict[str, Any]]:
    """Aggregate every source. Each loader is independently fault-isolated."""
    out: List[Dict[str, Any]] = []
    for loader in (_load_attestations, _load_approvals, _load_ledger, _load_dispatches):
        try:
            out.extend(loader() or [])
        except Exception:
            # A whole-source failure must not crash the queue.
            continue
    return out


def build_queue(
    class_filter: str = "",
    state_filter: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Build the unified, normalized, newest-first queue.

    Args:
        class_filter: "" (all) | "attestation" | "approval" | "sensing" | "ops".
        state_filter: "" (all) | a state string (e.g. "open", "pending",
            "awaiting_approval", "done"). Case-insensitive.
        limit: max items returned (default 100).

    Returns:
        A list of normalized item dicts (the `_raw` key is stripped here —
        get_item() exposes raw payloads). Sorted newest-first by `created`.

    Lane balancing (documented contract): when class_filter is empty, sensing
    items can vastly outnumber every other lane (~1170 STR-* items). To keep
    the moat (attestation) and approval lanes from being drowned, the unfiltered
    result reserves capacity per lane: attestations and approvals are never
    truncated by the sensing flood — they are taken in full (newest-first) up to
    `limit`, and the remaining budget is filled with ops then sensing items
    (each newest-first). A client wanting ONLY sensing should pass
    class_filter="sensing" to get the full sensing lane up to `limit`.
    """
    items = _all_items()

    cf = (class_filter or "").strip().lower()
    sf = (state_filter or "").strip().lower()

    if cf and cf in _VALID_CLASSES:
        items = [it for it in items if it.get("class") == cf]
    if sf:
        items = [it for it in items if str(it.get("state", "")).lower() == sf]

    # Strip internal keys from the list projection.
    def _public(it: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in it.items() if not k.startswith("_")}

    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 100
    if cap < 0:
        cap = 0

    # When a class filter is active (or no balancing needed), simple newest-first.
    if cf:
        items.sort(key=_sort_key, reverse=True)
        return [_public(it) for it in items[:cap]]

    # Unfiltered: balance lanes so sensing can't drown attestation/approval.
    lanes: Dict[str, List[Dict[str, Any]]] = {c: [] for c in _VALID_CLASSES}
    for it in items:
        c = it.get("class")
        if c in lanes:
            lanes[c].append(it)
    for c in lanes:
        lanes[c].sort(key=_sort_key, reverse=True)

    result: List[Dict[str, Any]] = []
    # Priority order: attestation (moat) + approval first, then ops, then sensing.
    for lane in ("attestation", "approval", "ops", "sensing"):
        for it in lanes[lane]:
            if len(result) >= cap:
                break
            result.append(it)
        if len(result) >= cap:
            break

    # Final newest-first sort across the balanced selection.
    result.sort(key=_sort_key, reverse=True)
    return [_public(it) for it in result[:cap]]


def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    """Return one normalized item by id, including its raw payload under `raw`.

    Args:
        item_id: the normalized item id (e.g. "att_023f00875109bc67",
            "STR-437", "LED-1709", "WO-2026-04-18-001", "DIR-679").

    Returns:
        The normalized dict (all public keys) plus a `raw` key holding the
        original source payload, or None if no item matches.
    """
    if not item_id:
        return None
    target = str(item_id)
    for it in _all_items():
        if str(it.get("id")) == target:
            out = {k: v for k, v in it.items() if not k.startswith("_")}
            out["raw"] = it.get("_raw")
            return out
    return None


def counts(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Compute counts_by_class and counts_by_state for a list of items."""
    by_class: Dict[str, int] = {}
    by_state: Dict[str, int] = {}
    for it in items:
        c = str(it.get("class", "unknown"))
        s = str(it.get("state", "unknown"))
        by_class[c] = by_class.get(c, 0) + 1
        by_state[s] = by_state.get(s, 0) + 1
    return {"counts_by_class": by_class, "counts_by_state": by_state}
