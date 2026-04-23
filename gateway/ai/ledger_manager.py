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


CENTRAL_LEDGER_DIR = Path.home() / ".delimit" / "ledger"


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
    """Get the ledger directory — ALWAYS uses central ~/.delimit/ledger/.

    Cross-model handoff fix: Codex and Gemini were writing to $PWD/.delimit/ledger/
    which caused ledger fragmentation. All models must use the same central location
    so Claude, Codex, and Gemini see the same items.

    The central ledger at ~/.delimit/ledger/ is the source of truth.
    Per-project .delimit/ dirs are for policies and config only, not ledger state.
    """
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
    """
    _src_norm = (source or "").strip().lower()
    _promoted_by = os.environ.get("_DELIMIT_SIGNAL_PROMOTED_BY", "")
    _guard_mode = os.environ.get("DELIMIT_SIGNAL_GUARD", "enforce").lower()
    if _src_norm.startswith("social_scan") or _src_norm.startswith("social_strategy"):
        if not _promoted_by:
            msg = (
                f"LED-877 guard: source={source!r} is a sensed observation, not "
                f"a ledger item. Use ai.sensing.signal_store.ingest() instead. "
                f"Promote explicitly via promote_to_ledger(signal_id=...)."
            )
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
                            "msg": msg,
                        }) + "\n")
                except Exception:
                    pass
                # fall through
            else:
                raise ValueError(msg)

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

    return {
        "added": result,
        "ledger": ledger,
        "venture": venture["name"],
        "total_items": len(_read_ledger(path)),
    }


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
) -> Dict[str, Any]:
    """Update an existing ledger item's fields."""
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
            update["labels"] = labels
        if blocked_by:
            update["blocked_by"] = blocked_by
        if blocks:
            update["blocks"] = blocks
        _append(path, update)

        # Sync to Supabase for dashboard visibility
        try:
            from ai.supabase_sync import sync_ledger_update
            sync_ledger_update(item_id, status=status or "", note=note or "")
        except Exception:
            pass  # Never let cloud sync break ledger operations

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
                    if "worked_by" in item:
                        state[item_id]["last_worked_by"] = item["worked_by"]
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
VALID_LINK_TYPES = {"blocks", "blocked_by", "parent", "child", "relates_to", "duplicates"}


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
    reverse_map = {"blocks": "blocked_by", "blocked_by": "blocks", "parent": "child", "child": "parent"}
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
