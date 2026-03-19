"""
Delimit Ledger Manager — Strategy + Operational ledger as first-class MCP tools.

Two ledgers:
- Strategy: consensus decisions, positioning, pricing, product direction
- Operational: tasks, bugs, features — the "keep building" items

Both are append-only JSONL at ~/.delimit/ledger/
"""

import json
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

LEDGER_DIR = Path.home() / ".delimit" / "ledger"
STRATEGY_LEDGER = LEDGER_DIR / "strategy.jsonl"
OPS_LEDGER = LEDGER_DIR / "operations.jsonl"


def _ensure():
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    for f in [STRATEGY_LEDGER, OPS_LEDGER]:
        if not f.exists():
            f.write_text("")


def _read_ledger(path: Path) -> List[Dict]:
    _ensure()
    items = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _append(path: Path, entry: Dict) -> Dict:
    _ensure()
    # Add hash chain
    items = _read_ledger(path)
    prev_hash = items[-1].get("hash", "genesis") if items else "genesis"
    entry["hash"] = hashlib.sha256(f"{prev_hash}{json.dumps(entry, sort_keys=True)}".encode()).hexdigest()[:16]
    entry["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def add_item(
    title: str,
    ledger: str = "ops",
    type: str = "task",
    priority: str = "P1",
    description: str = "",
    source: str = "session",
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Add a new item to the strategy or operational ledger."""
    path = STRATEGY_LEDGER if ledger == "strategy" else OPS_LEDGER

    # Auto-generate ID
    items = _read_ledger(path)
    prefix = "STR" if ledger == "strategy" else "LED"
    existing_ids = [i.get("id", "") for i in items]
    num = len(existing_ids) + 1
    # Find next available number
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
        "status": "open",
        "tags": tags or [],
    }

    result = _append(path, entry)
    return {
        "added": result,
        "ledger": ledger,
        "total_items": len(_read_ledger(path)),
    }


def update_item(
    item_id: str,
    status: Optional[str] = None,
    note: Optional[str] = None,
    priority: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing ledger item's status, priority, or add a note."""
    # Search both ledgers
    for ledger_name, path in [("ops", OPS_LEDGER), ("strategy", STRATEGY_LEDGER)]:
        items = _read_ledger(path)
        for item in items:
            if item.get("id") == item_id:
                # Append an update event
                update = {
                    "id": item_id,
                    "type": "update",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                if status:
                    update["status"] = status
                if note:
                    update["note"] = note
                if priority:
                    update["priority"] = priority
                _append(path, update)
                return {"updated": item_id, "changes": update, "ledger": ledger_name}

    return {"error": f"Item {item_id} not found in either ledger"}


def list_items(
    ledger: str = "both",
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List ledger items with optional filters."""
    results = {}

    for ledger_name, path in [("ops", OPS_LEDGER), ("strategy", STRATEGY_LEDGER)]:
        if ledger not in ("both", ledger_name):
            continue

        items = _read_ledger(path)

        # Build current state by replaying events
        state = {}
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
                    state[item_id]["updated_at"] = item.get("updated_at")
            else:
                state[item_id] = {**item}

        # Filter
        filtered = list(state.values())
        if status:
            filtered = [i for i in filtered if i.get("status") == status]
        if priority:
            filtered = [i for i in filtered if i.get("priority") == priority]

        # Sort by priority then created_at
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        filtered.sort(key=lambda x: (priority_order.get(x.get("priority", "P2"), 9), x.get("created_at", "")))

        results[ledger_name] = filtered[:limit]

    # Summary
    all_items = []
    for v in results.values():
        all_items.extend(v)

    open_count = sum(1 for i in all_items if i.get("status") == "open")
    done_count = sum(1 for i in all_items if i.get("status") == "done")

    return {
        "items": results,
        "summary": {
            "total": len(all_items),
            "open": open_count,
            "done": done_count,
            "in_progress": sum(1 for i in all_items if i.get("status") == "in_progress"),
        },
    }


def get_context() -> Dict[str, Any]:
    """Get a concise ledger summary for AI context — what's open, what's next."""
    result = list_items(status="open")
    open_items = []
    for ledger_items in result["items"].values():
        open_items.extend(ledger_items)

    # Sort by priority
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    open_items.sort(key=lambda x: priority_order.get(x.get("priority", "P2"), 9))

    return {
        "open_items": len(open_items),
        "next_up": [{"id": i["id"], "title": i["title"], "priority": i["priority"]}
                     for i in open_items[:5]],
        "summary": result["summary"],
        "tip": "Use delimit_ledger_add to add new items, delimit_ledger_done to mark complete.",
    }
