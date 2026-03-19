"""
Delimit Ledger Manager — Strategy + Operational ledger as first-class MCP tools.

Two ledgers per project:
- Strategy: consensus decisions, positioning, pricing, product direction
- Operational: tasks, bugs, features — the "keep building" items

Ledger lives at {project}/.delimit/ledger/ (project-local).
Ventures auto-registered at ~/.delimit/ventures.json on first use.
"""

import json
import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

GLOBAL_DIR = Path.home() / ".delimit"
VENTURES_FILE = GLOBAL_DIR / "ventures.json"


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
    """Silently register a venture in the global registry."""
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    ventures = {}
    if VENTURES_FILE.exists():
        try:
            ventures = json.loads(VENTURES_FILE.read_text())
        except Exception:
            pass

    name = info["name"]
    if name not in ventures:
        ventures[name] = {
            "path": info.get("path", ""),
            "repo": info.get("repo", ""),
            "type": info.get("type", ""),
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        VENTURES_FILE.write_text(json.dumps(ventures, indent=2))


def _project_ledger_dir(project_path: str = ".") -> Path:
    """Get the ledger directory for the current project."""
    p = Path(project_path).resolve()
    return p / ".delimit" / "ledger"


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


def add_item(
    title: str,
    ledger: str = "ops",
    type: str = "task",
    priority: str = "P1",
    description: str = "",
    source: str = "session",
    project_path: str = ".",
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Add a new item to the project's strategy or operational ledger."""
    _ensure(project_path)
    venture = _detect_venture(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    path = ledger_dir / ("strategy.jsonl" if ledger == "strategy" else "operations.jsonl")

    items = _read_ledger(path)
    prefix = "STR" if ledger == "strategy" else "LED"
    existing_ids = [i.get("id", "") for i in items if i.get("type") != "update"]
    num = len(existing_ids) + 1
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
    }

    result = _append(path, entry)
    return {
        "added": result,
        "ledger": ledger,
        "venture": venture["name"],
        "total_items": len(_read_ledger(path)),
    }


def update_item(
    item_id: str,
    status: Optional[str] = None,
    note: Optional[str] = None,
    priority: Optional[str] = None,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Update an existing ledger item's status, priority, or add a note."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)

    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        path = ledger_dir / filename
        items = _read_ledger(path)
        for item in items:
            if item.get("id") == item_id and item.get("type") != "update":
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

    return {"error": f"Item {item_id} not found in project ledger"}


def list_items(
    ledger: str = "both",
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
    project_path: str = ".",
) -> Dict[str, Any]:
    """List ledger items with optional filters."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    venture = _detect_venture(project_path)
    results = {}

    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        if ledger not in ("both", ledger_name):
            continue

        path = ledger_dir / filename
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

        filtered = list(state.values())
        if status:
            filtered = [i for i in filtered if i.get("status") == status]
        if priority:
            filtered = [i for i in filtered if i.get("priority") == priority]

        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        filtered.sort(key=lambda x: (priority_order.get(x.get("priority", "P2"), 9), x.get("created_at", "")))

        results[ledger_name] = filtered[:limit]

    all_items = []
    for v in results.values():
        all_items.extend(v)

    return {
        "venture": venture["name"],
        "items": results,
        "summary": {
            "total": len(all_items),
            "open": sum(1 for i in all_items if i.get("status") == "open"),
            "done": sum(1 for i in all_items if i.get("status") == "done"),
            "in_progress": sum(1 for i in all_items if i.get("status") == "in_progress"),
        },
    }


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
