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
from typing import Any, Dict, List, Optional

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


def _load_index(project_path: str) -> Dict[str, Any]:
    """Load the receipt index for a project."""
    idx_path = _index_path(project_path)
    if idx_path.exists():
        try:
            return json.loads(idx_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"receipts": []}


def _save_index(project_path: str, index: Dict[str, Any]) -> None:
    """Save the receipt index for a project."""
    proj_dir = _project_dir(project_path)
    proj_dir.mkdir(parents=True, exist_ok=True)
    _index_path(project_path).write_text(json.dumps(index, indent=2))


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
) -> HandoffReceipt:
    """Create a handoff receipt and persist it to disk.

    Auto-detects project_path from cwd and files_modified from git if not provided.
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
    )

    _store_receipt(receipt)
    return receipt


def _load_receipt(project_path: str, receipt_id: str) -> Optional[HandoffReceipt]:
    """Load a receipt from disk by ID."""
    filepath = _project_dir(project_path) / f"{receipt_id}.json"
    if not filepath.exists():
        return None
    try:
        data = json.loads(filepath.read_text())
        return HandoffReceipt(**{
            k: v for k, v in data.items()
            if k in HandoffReceipt.__dataclass_fields__
        })
    except (json.JSONDecodeError, TypeError, KeyError, OSError):
        return None


def acknowledge_receipt(
    receipt_id: str,
    model: str = "unknown",
    notes: str = "",
    project_path: str = "",
) -> Dict[str, Any]:
    """Mark a handoff receipt as acknowledged by the receiving agent.

    Returns the updated receipt data or an error if not found.
    """
    project_path = project_path or os.getcwd()

    receipt = _load_receipt(project_path, receipt_id)
    if receipt is None:
        return {
            "status": "not_found",
            "message": f"No receipt with ID '{receipt_id}' found.",
        }

    if receipt.acknowledged:
        return {
            "status": "already_acknowledged",
            "message": f"Receipt {receipt_id} was already acknowledged by {receipt.acknowledged_by} at {receipt.acknowledged_at}.",
            "receipt_id": receipt_id,
        }

    now = datetime.now(timezone.utc).isoformat()
    receipt.acknowledged = True
    receipt.acknowledged_at = now
    receipt.acknowledged_by = model
    receipt.acknowledge_notes = notes

    # Update the receipt file
    filepath = _project_dir(project_path) / f"{receipt_id}.json"
    filepath.write_text(json.dumps(asdict(receipt), indent=2))

    # Update the index
    index = _load_index(project_path)
    for entry in index["receipts"]:
        if entry["receipt_id"] == receipt_id:
            entry["acknowledged"] = True
            break
    _save_index(project_path, index)

    return {
        "status": "acknowledged",
        "receipt_id": receipt_id,
        "acknowledged_by": model,
        "acknowledged_at": now,
        "task_description": receipt.task_description,
        "next_action": receipt.next_action,
        "message": f"Receipt {receipt_id} acknowledged. Next action: {receipt.next_action or '(none specified)'}",
    }


def get_pending_receipts(project_path: str = "") -> List[HandoffReceipt]:
    """Get receipts that haven't been acknowledged yet."""
    project_path = project_path or os.getcwd()
    index = _load_index(project_path)

    pending = []
    for entry in index["receipts"]:
        if not entry.get("acknowledged", False):
            receipt = _load_receipt(project_path, entry["receipt_id"])
            if receipt and not receipt.acknowledged:
                pending.append(receipt)
    return pending


def get_receipts(project_path: str = "", status: str = "pending") -> List[HandoffReceipt]:
    """Get receipts filtered by status: pending, acknowledged, or all."""
    project_path = project_path or os.getcwd()
    index = _load_index(project_path)

    results = []
    for entry in index["receipts"]:
        receipt = _load_receipt(project_path, entry["receipt_id"])
        if receipt is None:
            continue
        if status == "all":
            results.append(receipt)
        elif status == "pending" and not receipt.acknowledged:
            results.append(receipt)
        elif status == "acknowledged" and receipt.acknowledged:
            results.append(receipt)
    return results


def format_receipt(receipt: HandoffReceipt) -> str:
    """Format a receipt into a clean, readable text block."""
    lines = []
    lines.append("=== HANDOFF RECEIPT ===")
    lines.append(f"ID: {receipt.receipt_id}")
    lines.append(f"From: {receipt.from_model} | To: {receipt.to_model}")
    lines.append(f"Task: {receipt.task_description}")
    lines.append(f"Priority: {receipt.priority}")
    lines.append(f"Created: {receipt.created_at}")
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
