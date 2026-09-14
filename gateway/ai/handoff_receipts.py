"""
Handoff Receipts -- Structured receipts for agent-to-agent handoffs (LED-220).

When one agent/session hands off to another, a receipt captures:
- What was done and what wasn't
- Assumptions made and blockers encountered
- Files touched with change summaries
- Scope boundaries
- Next action required

The receiving agent must acknowledge the receipt before acting,
preventing the "undo what the last agent did" problem.

Architecture:
  create_receipt()      -> ~/.delimit/handoff_receipts/{project_hash}/{receipt_id}.json
  acknowledge_receipt() -> marks receipt as acknowledged
  get_pending_receipts()-> returns unacknowledged receipts
"""

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_RECEIPTS_PER_PROJECT = 50
RECEIPTS_BASE_DIR = Path.home() / ".delimit" / "handoff_receipts"


@dataclass
class HandoffReceipt:
    """Structured receipt for agent-to-agent handoffs."""

    receipt_id: str = ""
    created_at: str = ""
    from_model: str = "unknown"
    to_model: str = "any"
    project_path: str = ""

    # Work summary
    task_description: str = ""
    completed: List[str] = field(default_factory=list)
    not_completed: List[str] = field(default_factory=list)

    # Context transfer
    assumptions: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    # File manifest: [{path, change_type, summary}]
    files_modified: List[Dict[str, str]] = field(default_factory=list)

    # Scope
    in_scope: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)

    # Next action
    next_action: str = ""
    priority: str = "P1"

    # Acknowledgment
    acknowledged: bool = False
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    acknowledge_notes: str = ""

    # Canonical-close linkage (LED-5321 M3). Additive only — legacy
    # receipts on disk deserialize unchanged (unknown keys are already
    # filtered on load, missing keys fall back to these defaults).
    logical_session_id: str = ""
    soul_id: str = ""
    handoff_id: str = ""
    # LED-5321 M4: integrity + origin (all optional, backward compatible).
    # content_digest is sha256 over the canonical JSON of the receipt body
    # (ack fields excluded); origin records who created it. integrity/stale/
    # owner_backed are COMPUTED on read, never trusted from storage.
    content_digest: str = ""
    created_by_model: str = ""
    created_by_session: str = ""
    integrity: str = "legacy"
    stale: bool = False
    owner_backed: bool = False


# LED-5321 M4: body fields covered by the content digest (ack + computed
# fields excluded so acknowledge() never invalidates the digest).
_RECEIPT_BODY_FIELDS = (
    "receipt_id",
    "created_at",
    "from_model",
    "to_model",
    "project_path",
    "task_description",
    "completed",
    "not_completed",
    "assumptions",
    "blockers",
    "files_modified",
    "in_scope",
    "out_of_scope",
    "next_action",
    "priority",
    "created_by_model",
    "created_by_session",
)


def _receipt_body_dict(data: Any) -> Dict[str, Any]:
    if isinstance(data, HandoffReceipt):
        raw = asdict(data)
    elif isinstance(data, dict):
        raw = data
    else:
        return {}
    return {k: raw.get(k) for k in _RECEIPT_BODY_FIELDS}


def _compute_content_digest(body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_receipt_integrity(raw: Dict[str, Any]) -> str:
    """Return ok|mismatch|legacy for a stored receipt dict (LED-5321 M4)."""
    if not isinstance(raw, dict):
        return "legacy"
    stored = raw.get("content_digest", "")
    if not stored:
        return "legacy"
    expected = _compute_content_digest(_receipt_body_dict(raw))
    return "ok" if stored == expected else "mismatch"


def _parse_created_at(value: str) -> str:
    # Lexical ISO compare is sufficient (all writers use ISO-8601 UTC);
    # normalize minimally so mixed naive/aware strings still order.
    return str(value or "")


def _is_receipt_stale(project_dir: Path, receipt: HandoffReceipt) -> bool:
    """True when a newer receipt for the same task exists (LED-5321 M4).

    Same task = identical stripped task_description within the same project
    namespace. Compares created_at; unreadable stores fail open (not stale)
    so a corrupt index never blocks an ack — integrity still gates.
    """
    try:
        task = (receipt.task_description or "").strip()
        if not task:
            return False
        newest = _parse_created_at(receipt.created_at)
        index = _load_index_from_dir(project_dir)
        entries = index.get("receipts", []) if isinstance(index, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (entry.get("task_description") or "").strip() != task:
                continue
            other_ts = _parse_created_at(str(entry.get("created_at", "")))
            if other_ts > newest:
                return True
        return False
    except Exception:
        return False


def truth_label_for_receipt(receipt: HandoffReceipt) -> str:
    """Map a receipt to ASSERTED/VERIFIED/STALE/UNAVAILABLE/legacy (M4)."""
    try:
        integrity = getattr(receipt, "integrity", "legacy")
        if integrity == "mismatch":
            return "UNAVAILABLE"
        if integrity == "legacy":
            return "legacy/unclassified"
        if bool(getattr(receipt, "stale", False)):
            return "STALE"
        if bool(getattr(receipt, "acknowledged", False)):
            return "VERIFIED"
        return "ASSERTED"
    except Exception:
        return "legacy/unclassified"


def _load_owner_action_records(owner_actions_path: Any = None) -> List[Dict[str, Any]]:
    try:
        if owner_actions_path is not None:
            path = Path(str(owner_actions_path))
        else:
            from ai import notify as _notify

            path = Path(str(_notify.OWNER_ACTIONS_FILE))
    except Exception:
        return []
    try:
        if not path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                records.append(row)
        return records
    except (OSError, ValueError):
        return []


def check_owner_backed(
    note: str = "",
    text: str = "",
    decision_ref: str = "",
    sender: str = "",
    receipt: Optional[HandoffReceipt] = None,
    owner_actions_path: Any = None,
) -> Dict[str, Any]:
    """Whether an owner decision is backed by an owner_action record (M4).

    Owner identity/grants come ONLY from authenticated mechanisms
    (ai.notify.is_founder_sender + owner_action records), never from a
    free-text actor string. A decision is backed when its text (or an
    explicit decision_ref) references an interaction_id present in the
    owner-actions store. Free-text "owner approved" with no record link
    returns owner_backed=False. Deterministic, no model calls.
    """
    parts: List[str] = []
    if note:
        parts.append(str(note))
    if text:
        parts.append(str(text))
    if decision_ref:
        parts.append(str(decision_ref))
    if receipt is not None:
        try:
            for key in ("task_description", "next_action"):
                val = getattr(receipt, key, "")
                if val:
                    parts.append(str(val))
            for key in ("completed", "not_completed", "assumptions", "blockers"):
                vals = getattr(receipt, key, []) or []
                for v in vals:
                    if v:
                        parts.append(str(v))
        except Exception:
            pass
    blob = "\n".join(parts)
    records = _load_owner_action_records(owner_actions_path)
    if decision_ref:
        ref = str(decision_ref).strip()
        for rec in records:
            try:
                if str(rec.get("interaction_id") or "").strip() == ref and ref:
                    return {"owner_backed": True, "reason": "interaction_id_match",
                            "interaction_id": ref}
            except Exception:
                continue
        return {"owner_backed": False, "reason": "no_matching_owner_action_record"}
    if blob:
        for rec in records:
            try:
                iid = str(rec.get("interaction_id") or "").strip()
            except Exception:
                continue
            if iid and iid in blob:
                return {"owner_backed": True, "reason": "interaction_id_match",
                        "interaction_id": iid}
    # A founder sender string alone never grants backing — it must link to
    # a durable owner_action record (checked above).
    if sender:
        try:
            from ai.notify import is_founder_sender as _is_founder

            if not _is_founder(sender):
                return {"owner_backed": False, "reason": "sender_not_founder_no_record"}
        except Exception:
            pass
    return {"owner_backed": False, "reason": "no_matching_owner_action_record"}


def is_owner_backed(
    note: str = "",
    text: str = "",
    decision_ref: str = "",
    sender: str = "",
    receipt: Optional[HandoffReceipt] = None,
    owner_actions_path: Any = None,
) -> bool:
    """Boolean convenience wrapper for check_owner_backed (LED-5321 M4)."""
    try:
        return bool(check_owner_backed(
            note=note, text=text, decision_ref=decision_ref,
            sender=sender, receipt=receipt,
            owner_actions_path=owner_actions_path,
        ).get("owner_backed"))
    except Exception:
        return False


def _project_hash(project_path: str) -> str:
    """Stable hash for a project path, used as directory name."""
    normalized = os.path.realpath(project_path)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Return the receipt storage directory for a project."""
    return RECEIPTS_BASE_DIR / _project_hash(project_path)


def _run_git(args: List[str], cwd: str = "") -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _auto_detect_files(project_path: str) -> List[Dict[str, str]]:
    """Auto-detect modified files from git diff HEAD~1."""
    cwd = project_path or os.getcwd()

    # Get files changed in the last commit
    diff_output = _run_git(["diff", "--name-status", "HEAD~1"], cwd=cwd)
    if not diff_output:
        # Fall back to uncommitted changes
        diff_output = _run_git(["diff", "--name-status", "HEAD"], cwd=cwd)
    if not diff_output:
        return []

    files = []
    status_map = {
        "A": "created",
        "M": "modified",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
    }
    for line in diff_output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) >= 2:
            status_code = parts[0].strip()[0] if parts[0].strip() else "M"
            filepath = parts[1].strip()
            change_type = status_map.get(status_code, "modified")
            files.append({
                "path": filepath,
                "change_type": change_type,
                "summary": "",
            })
    return files


def _index_path(project_path: str) -> Path:
    """Return the index file path for a project."""
    return _project_dir(project_path) / "index.json"


def _load_index_from_dir(project_dir: Path) -> Dict[str, Any]:
    """Load and validate the index for an already-resolved namespace."""
    try:
        data = json.loads((project_dir / "index.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"receipts": []}
    if not isinstance(data, dict) or not isinstance(data.get("receipts"), list):
        return {"receipts": []}
    return data


def _load_index(project_path: str) -> Dict[str, Any]:
    """Load the receipt index for a project."""
    return _load_index_from_dir(_project_dir(project_path))


def _save_index_to_dir(project_dir: Path, index: Dict[str, Any]) -> None:
    """Persist an index to an already-resolved namespace."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "index.json").write_text(json.dumps(index, indent=2))


def _save_index(project_path: str, index: Dict[str, Any]) -> None:
    """Save the receipt index for a project."""
    _save_index_to_dir(_project_dir(project_path), index)


def create_receipt(
    task_description: str,
    completed: Optional[List[str]] = None,
    not_completed: Optional[List[str]] = None,
    assumptions: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    files_modified: Optional[List[Dict[str, str]]] = None,
    in_scope: Optional[List[str]] = None,
    out_of_scope: Optional[List[str]] = None,
    next_action: str = "",
    priority: str = "P1",
    from_model: str = "unknown",
    to_model: str = "any",
    project_path: str = "",
    # LED-5321 M4: all optional, backward compatible.
    created_by_model: str = "",
    created_by_session: str = "",
) -> HandoffReceipt:
    """Create a handoff receipt and persist it to disk.

    Auto-detects project_path from cwd and files_modified from git if not provided.

    LED-5321 M4: stores a content digest (sha256 over the canonical JSON of
    the receipt body) and the origin (created_by_model/session/created_at).
    """
    project_path = project_path or os.getcwd()

    if files_modified is None:
        files_modified = _auto_detect_files(project_path)

    receipt = HandoffReceipt(
        receipt_id=str(uuid.uuid4())[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        from_model=from_model,
        to_model=to_model,
        project_path=project_path,
        task_description=task_description,
        completed=completed or [],
        not_completed=not_completed or [],
        assumptions=assumptions or [],
        blockers=blockers or [],
        files_modified=files_modified,
        in_scope=in_scope or [],
        out_of_scope=out_of_scope or [],
        next_action=next_action,
        priority=priority,
        created_by_model=created_by_model or from_model,
        created_by_session=created_by_session
        or os.environ.get("DELIMIT_SESSION_ID", "")
        or os.environ.get("DELIMIT_SESSION", ""),
    )
    receipt.content_digest = _compute_content_digest(_receipt_body_dict(receipt))
    receipt.integrity = "ok"
    receipt.stale = False

    _store_receipt(receipt)
    return receipt


def _store_receipt(receipt: HandoffReceipt) -> Path:
    """Persist a receipt to disk and update the index."""
    proj_dir = _project_dir(receipt.project_path)
    proj_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{receipt.receipt_id}.json"
    filepath = proj_dir / filename
    filepath.write_text(json.dumps(asdict(receipt), indent=2))

    # Update index
    index = _load_index(receipt.project_path)
    index["receipts"].append({
        "receipt_id": receipt.receipt_id,
        "created_at": receipt.created_at,
        "task_description": receipt.task_description,
        "from_model": receipt.from_model,
        "to_model": receipt.to_model,
        "priority": receipt.priority,
        "acknowledged": False,
    })

    # Prune old receipts
    if len(index["receipts"]) > MAX_RECEIPTS_PER_PROJECT:
        old_entries = index["receipts"][:-MAX_RECEIPTS_PER_PROJECT]
        index["receipts"] = index["receipts"][-MAX_RECEIPTS_PER_PROJECT:]
        for entry in old_entries:
            old_file = proj_dir / f"{entry['receipt_id']}.json"
            old_file.unlink(missing_ok=True)

    _save_index(receipt.project_path, index)
    return filepath


def _load_receipt_file(filepath: Path) -> Optional[HandoffReceipt]:
    """Load a receipt from an already-resolved file path.

    LED-5321 M4: recomputes integrity from the stored body (never trusts a
    stored integrity value). Legacy receipts without a digest load with
    integrity="legacy".
    """
    if not filepath.exists():
        return None
    try:
        data = json.loads(filepath.read_text())
        receipt = HandoffReceipt(**{
            k: v for k, v in data.items()
            if k in HandoffReceipt.__dataclass_fields__
            and k not in ("integrity", "stale", "owner_backed")
        })
    except (json.JSONDecodeError, TypeError, KeyError, OSError):
        return None
    try:
        receipt.integrity = _verify_receipt_integrity(data if isinstance(data, dict) else {})
    except Exception:
        receipt.integrity = "legacy"
    receipt.stale = False
    receipt.owner_backed = False
    return receipt


def _load_receipt(project_path: str, receipt_id: str) -> Optional[HandoffReceipt]:
    """Load a receipt from one explicit project namespace."""
    return _load_receipt_file(_project_dir(project_path) / f"{receipt_id}.json")


def _receipt_project_dirs(project_path: str = "") -> List[Path]:
    """Resolve one explicit namespace, or every namespace when blank."""
    if project_path:
        return [_project_dir(project_path)]
    if not RECEIPTS_BASE_DIR.exists():
        return []
    try:
        entries = sorted(RECEIPTS_BASE_DIR.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    project_dirs = []
    for path in entries:
        try:
            if path.is_dir():
                project_dirs.append(path)
        except OSError:
            continue
    return project_dirs


def _find_receipts(
    receipt_id: str,
    project_path: str = "",
    fallback_global: bool = True,
) -> List[Tuple[Path, HandoffReceipt]]:
    """Find receipt matches, preferring an explicit namespace when it hits.

    A supplied project path is a fast path, not an isolation boundary: scoped
    revive historically falls back to the global receipt store when a handoff
    was captured from a different cwd. Preserve that behavior while making
    list and acknowledge use the same resolver contract.
    """
    if not receipt_id:
        return []

    if project_path:
        explicit_matches = []
        for project_dir in _receipt_project_dirs(project_path):
            receipt = _load_receipt_file(project_dir / f"{receipt_id}.json")
            if receipt is not None:
                explicit_matches.append((project_dir, receipt))
        if explicit_matches or not fallback_global:
            return explicit_matches

    matches = []
    for project_dir in _receipt_project_dirs():
        receipt = _load_receipt_file(project_dir / f"{receipt_id}.json")
        if receipt is not None:
            matches.append((project_dir, receipt))
    return matches


def get_receipt(receipt_id: str, project_path: str = "") -> Optional[HandoffReceipt]:
    """Look up a single receipt by id.

    Receipts are stored per project-hash, so a bare receipt id has no
    single canonical directory. When ``project_path`` is given we read the
    fast path (``_load_receipt`` in that project's dir). On an explicit miss
    we scan every project-hash directory under ``RECEIPTS_BASE_DIR`` for a
    matching ``{receipt_id}.json``. Read-only; returns None when no receipt
    exists or a bare id is ambiguous across namespaces.

    LED-5321 M4: verifies the content digest and flags
    integrity ok|mismatch|legacy plus staleness (older than the newest
    receipt for the same task). Never mutates storage.
    """
    matches = _find_receipts(receipt_id, project_path=project_path)
    if len(matches) != 1:
        return None
    project_dir, receipt = matches[0]
    try:
        receipt.stale = _is_receipt_stale(project_dir, receipt)
    except Exception:
        receipt.stale = False
    try:
        receipt.owner_backed = is_owner_backed(receipt=receipt)
    except Exception:
        receipt.owner_backed = False
    return receipt


def acknowledge_receipt(
    receipt_id: str,
    model: str = "unknown",
    notes: str = "",
    project_path: str = "",
    # LED-5321 M4: optional, backward compatible.
    allow_stale: bool = False,
) -> Dict[str, Any]:
    """Mark a handoff receipt as acknowledged by the receiving agent.

    Returns the updated receipt data or an error if not found.

    LED-5321 M4: refuses a receipt whose stored digest mismatches (forged
    or tampered body) and a stale receipt (older than the newest receipt
    for the same task) unless allow_stale=True is passed explicitly.
    Never rewrites the stored receipt body — only ack fields change.
    """
    matches = _find_receipts(
        receipt_id,
        project_path=project_path,
        fallback_global=not bool(project_path),
    )
    if not matches:
        return {
            "status": "not_found",
            "message": f"No receipt with ID '{receipt_id}' found.",
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "message": (
                f"Receipt ID '{receipt_id}' exists in multiple project "
                "namespaces; pass project_path."
            ),
        }
    project_dir, receipt = matches[0]

    integrity = getattr(receipt, "integrity", "legacy")
    if integrity == "mismatch":
        return {
            "status": "integrity_mismatch",
            "message": (
                f"Receipt {receipt_id} failed integrity verification "
                "(stored digest mismatch — body may be forged or tampered); "
                "acknowledgment refused."
            ),
            "receipt_id": receipt_id,
            "integrity": "mismatch",
        }

    try:
        receipt.stale = _is_receipt_stale(project_dir, receipt)
    except Exception:
        receipt.stale = False
    if receipt.stale and not allow_stale:
        return {
            "status": "stale",
            "message": (
                f"Receipt {receipt_id} is stale (a newer receipt exists for "
                "the same task); pass allow_stale=True to acknowledge anyway."
            ),
            "receipt_id": receipt_id,
            "integrity": integrity,
            "stale": True,
        }

    if receipt.acknowledged:
        return {
            "status": "already_acknowledged",
            "message": f"Receipt {receipt_id} was already acknowledged by {receipt.acknowledged_by} at {receipt.acknowledged_at}.",
            "receipt_id": receipt_id,
            "integrity": integrity,
            "stale": bool(receipt.stale),
        }

    now = datetime.now(timezone.utc).isoformat()

    # Update the receipt file — ack fields ONLY; the stored body
    # (including content_digest and origin) is preserved byte-for-byte.
    filepath = project_dir / f"{receipt_id}.json"
    try:
        raw = json.loads(filepath.read_text())
        if not isinstance(raw, dict):
            raw = asdict(receipt)
    except (OSError, ValueError):
        raw = asdict(receipt)
    raw["acknowledged"] = True
    raw["acknowledged_at"] = now
    raw["acknowledged_by"] = model
    raw["acknowledge_notes"] = notes
    filepath.write_text(json.dumps(raw, indent=2))
    receipt.acknowledged = True
    receipt.acknowledged_at = now
    receipt.acknowledged_by = model
    receipt.acknowledge_notes = notes

    # Update the index
    index = _load_index_from_dir(project_dir)
    found_in_index = False
    for entry in index["receipts"]:
        if isinstance(entry, dict) and entry.get("receipt_id") == receipt_id:
            entry["acknowledged"] = True
            found_in_index = True
    if not found_in_index:
        index["receipts"].append(
            {
                "receipt_id": receipt.receipt_id,
                "created_at": receipt.created_at,
                "task_description": receipt.task_description,
                "from_model": receipt.from_model,
                "to_model": receipt.to_model,
                "priority": receipt.priority,
                "acknowledged": True,
            }
        )
    _save_index_to_dir(project_dir, index)

    _ack_owner = False
    try:
        _ack_owner = is_owner_backed(receipt=receipt)
    except Exception:
        _ack_owner = False
    return {
        "status": "acknowledged",
        "receipt_id": receipt_id,
        "acknowledged_by": model,
        "acknowledged_at": now,
        "task_description": receipt.task_description,
        "next_action": receipt.next_action,
        "message": f"Receipt {receipt_id} acknowledged. Next action: {receipt.next_action or '(none specified)'}",
        "integrity": integrity,
        "stale": bool(receipt.stale),
        "owner_backed": bool(_ack_owner),
    }


def get_pending_receipts(project_path: str = "") -> List[HandoffReceipt]:
    """Get receipts that haven't been acknowledged yet."""
    return get_receipts(project_path=project_path, status="pending")


def get_receipts(project_path: str = "", status: str = "pending") -> List[HandoffReceipt]:
    """Get receipts filtered by status: pending, acknowledged, or all.

    Explicit project paths stay namespace-local. A blank path aggregates all
    namespaces, newest first. MCP callers should bound and paginate display.
    """
    results = []
    seen = set()
    for project_dir in _receipt_project_dirs(project_path):
        index = _load_index_from_dir(project_dir)
        for entry in index["receipts"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("receipt_id"), str):
                continue
            key = (project_dir, entry["receipt_id"])
            if key in seen:
                continue
            seen.add(key)
            receipt = _load_receipt_file(project_dir / f"{entry['receipt_id']}.json")
            if receipt is None:
                continue
            # LED-5321 M4: flag staleness per receipt (index scan, no body reads).
            try:
                receipt.stale = _is_receipt_stale(project_dir, receipt)
            except Exception:
                receipt.stale = False
            if (
                status == "all"
                or (status == "pending" and not receipt.acknowledged)
                or (status == "acknowledged" and receipt.acknowledged)
            ):
                results.append(receipt)
    results.sort(key=lambda receipt: (receipt.created_at, receipt.receipt_id), reverse=True)
    return results


def format_receipt(receipt: HandoffReceipt) -> str:
    """Format a receipt into a clean, readable text block.

    LED-5321 M4: labels the receipt ASSERTED/VERIFIED/STALE/UNAVAILABLE
    where integrity data exists; legacy receipts without a digest show
    "legacy/unclassified" (never fabricated).
    """
    lines = []
    lines.append("=== HANDOFF RECEIPT ===")
    lines.append(f"ID: {receipt.receipt_id}")
    lines.append(f"From: {receipt.from_model} | To: {receipt.to_model}")
    lines.append(f"Task: {receipt.task_description}")
    lines.append(f"Priority: {receipt.priority}")
    lines.append(f"Created: {receipt.created_at}")
    try:
        _integrity = getattr(receipt, "integrity", "legacy") or "legacy"
    except Exception:
        _integrity = "legacy"
    try:
        _truth = truth_label_for_receipt(receipt)
    except Exception:
        _truth = "legacy/unclassified"
    try:
        _stale = bool(getattr(receipt, "stale", False))
    except Exception:
        _stale = False
    try:
        _backed = bool(getattr(receipt, "owner_backed", False))
        if not _backed:
            _backed = is_owner_backed(receipt=receipt)
    except Exception:
        _backed = False
    lines.append(f"Integrity: {_integrity} | Truth: {_truth} | Stale: {'yes' if _stale else 'no'} | Owner-backed: {'true' if _backed else 'false'}")
    try:
        _cbm = getattr(receipt, "created_by_model", "") or ""
        _cbs = getattr(receipt, "created_by_session", "") or ""
        if _cbm or _cbs:
            lines.append(f"Origin: model={_cbm or '?'} session={_cbs or '?'}")
    except Exception:
        pass
    lines.append("")

    if receipt.completed:
        lines.append("COMPLETED:")
        for item in receipt.completed:
            lines.append(f"  [x] {item}")
        lines.append("")

    if receipt.not_completed:
        lines.append("NOT COMPLETED:")
        for item in receipt.not_completed:
            lines.append(f"  [ ] {item}")
        lines.append("")

    if receipt.assumptions:
        lines.append("ASSUMPTIONS:")
        for item in receipt.assumptions:
            lines.append(f"  - {item}")
        lines.append("")

    if receipt.blockers:
        lines.append("BLOCKERS:")
        for item in receipt.blockers:
            lines.append(f"  ! {item}")
        lines.append("")

    if receipt.files_modified:
        lines.append("FILES MODIFIED:")
        for f in receipt.files_modified:
            path = f.get("path", "")
            change_type = f.get("change_type", "modified")
            summary = f.get("summary", "")
            suffix = f" -- {summary}" if summary else ""
            lines.append(f"  {path} ({change_type}){suffix}")
        lines.append("")

    if receipt.in_scope:
        lines.append("IN SCOPE:")
        for item in receipt.in_scope:
            lines.append(f"  + {item}")
        lines.append("")

    if receipt.out_of_scope:
        lines.append("OUT OF SCOPE:")
        for item in receipt.out_of_scope:
            lines.append(f"  - {item}")
        lines.append("")

    if receipt.next_action:
        lines.append(f"NEXT ACTION: {receipt.next_action}")
        lines.append("")

    if receipt.acknowledged:
        lines.append(f"ACKNOWLEDGED: by {receipt.acknowledged_by} at {receipt.acknowledged_at}")
        if receipt.acknowledge_notes:
            lines.append(f"  Notes: {receipt.acknowledge_notes}")
        lines.append("")
    else:
        lines.append(f'To acknowledge: delimit_handoff_acknowledge(receipt_id="{receipt.receipt_id}")')
        lines.append("")

    lines.append("=" * 24)
    return "\n".join(lines)


# ── LED-5321 M3: canonical-close idempotent receipt upkeep ────────────────


def find_receipts_by_logical_id(
    logical_session_id: str,
    project_path: str = "",
) -> List[HandoffReceipt]:
    """Return receipts stamped with ``logical_session_id``, newest first.

    Scoped to ``project_path``'s namespace when given, else aggregated
    across namespaces (mirrors :func:`get_receipts`). Read-only; legacy
    receipts without the linkage field never match.
    """
    if not logical_session_id:
        return []
    matches = [
        receipt
        for receipt in get_receipts(project_path=project_path, status="all")
        if getattr(receipt, "logical_session_id", "") == logical_session_id
    ]
    matches.sort(
        key=lambda receipt: (receipt.created_at, receipt.receipt_id),
        reverse=True,
    )
    return matches


def update_receipt_fields(
    receipt_id: str,
    updates: Dict[str, Any],
    project_path: str = "",
) -> Optional[HandoffReceipt]:
    """Update one receipt's mutable fields in place (same receipt_id).

    Used by the canonical close for idempotent re-close: the receipt keeps
    its identity while its content is refreshed. ``receipt_id``,
    ``created_at``, and acknowledgment state are never overwritten here.
    The namespace index entry is refreshed to match. Returns the updated
    receipt, or None when the id is missing or ambiguous.
    """
    if not receipt_id:
        return None
    matches = _find_receipts(
        receipt_id,
        project_path=project_path,
        fallback_global=not bool(project_path),
    )
    if len(matches) != 1:
        return None
    project_dir, receipt = matches[0]
    protected = {
        "receipt_id",
        "created_at",
        "acknowledged",
        "acknowledged_at",
        "acknowledged_by",
        "acknowledge_notes",
    }
    for key, value in (updates or {}).items():
        if key in protected or key not in HandoffReceipt.__dataclass_fields__:
            continue
        setattr(receipt, key, value)
    filepath = project_dir / f"{receipt_id}.json"
    filepath.write_text(json.dumps(asdict(receipt), indent=2))
    index = _load_index_from_dir(project_dir)
    for entry in index.get("receipts", []):
        if isinstance(entry, dict) and entry.get("receipt_id") == receipt_id:
            entry["task_description"] = receipt.task_description
            entry["from_model"] = receipt.from_model
            entry["to_model"] = receipt.to_model
            entry["priority"] = receipt.priority
    _save_index_to_dir(project_dir, index)
    return receipt
