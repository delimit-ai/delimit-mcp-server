"""
Memory bridge — file-based semantic memory store.
Stores memories as JSON files in ~/.delimit/memory/.
"""

import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.memory_bridge")

MEMORY_DIR = Path.home() / ".delimit" / "memory"


def _ensure_dir():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def store(
    content: str,
    tags: Optional[list] = None,
    context: Optional[str] = None,
    hot_load: bool = False,
) -> Dict[str, Any]:
    """Store a memory entry.

    LED-1165 Phase 2 #5 PR-A: opt-in `hot_load` flag marks an entry for
    one-way projection into the Claude Code auto-memory `MEMORY.md` file
    (managed-section). The projection writer is shipped in PR-B; this PR
    only persists the flag.

    Args:
        content: The content to remember.
        tags: Optional categorization tags.
        context: Optional context about when/why this was stored.
        hot_load: When True, mark the entry for projection into the
            Claude Code MEMORY.md hot-load index. Default False — entries
            are durable in delimit_memory but not projected. Existing
            entries are unaffected (treated as hot_load=False).
    """
    _ensure_dir()

    # Generate ID from content hash
    mem_id = "mem-" + hashlib.sha256(content[:100].encode()).hexdigest()[:12]
    ts = datetime.now(timezone.utc).isoformat()

    entry = {
        "id": mem_id,
        "content": content,
        "tags": tags or [],
        "context": context or "",
        "created_at": ts,
        "hot_load": bool(hot_load),
    }

    path = MEMORY_DIR / f"{mem_id}.json"
    path.write_text(json.dumps(entry, indent=2))

    return {
        "stored": mem_id,
        "path": str(path),
        "created_at": ts,
        "hot_load": bool(hot_load),
    }


def search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search memories by keyword matching."""
    _ensure_dir()
    query_lower = query.lower()
    results = []

    for f in sorted(MEMORY_DIR.glob("*.json"), reverse=True):
        try:
            entry = json.loads(f.read_text())
            content = entry.get("content", "").lower()
            tags = " ".join(entry.get("tags", [])).lower()
            context = entry.get("context", "").lower()

            # Simple keyword matching
            if query_lower in content or query_lower in tags or query_lower in context:
                results.append({
                    "id": entry.get("id", f.stem),
                    "content": entry.get("content", "")[:500],
                    "tags": entry.get("tags", []),
                    "created_at": entry.get("created_at", ""),
                    "relevance": content.count(query_lower),
                })

            if len(results) >= limit:
                break
        except Exception:
            pass

    results.sort(key=lambda r: r.get("relevance", 0), reverse=True)
    return {"query": query, "results": results, "count": len(results)}


def get_recent(limit: int = 5) -> Dict[str, Any]:
    """Get recent memory entries."""
    _ensure_dir()
    entries = []

    for f in sorted(MEMORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(entries) >= limit:
            break
        try:
            entry = json.loads(f.read_text())
            entries.append({
                "id": entry.get("id", f.stem),
                "content": entry.get("content", "")[:500],
                "tags": entry.get("tags", []),
                "created_at": entry.get("created_at", ""),
                "hot_load": bool(entry.get("hot_load", False)),
            })
        except Exception:
            pass

    return {"results": entries, "count": len(entries)}


def list_hot(limit: int = 200) -> Dict[str, Any]:
    """Return all entries marked hot_load=True, newest first.

    LED-1165 Phase 2 #5 PR-A: backing query for the MEMORY.md projection
    writer that PR-B will introduce. Returned entries are full content
    (not truncated) so the projection writer can render them faithfully.

    Args:
        limit: cap on entries returned. Default 200; the projection
            writer hard-caps the rendered MEMORY.md size so this is
            mostly belt-and-braces.

    Returns:
        {
            "results": [{id, content, tags, context, created_at, hot_load}, ...],
            "count": int,
        }
    """
    _ensure_dir()
    entries = []

    for f in sorted(MEMORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(entries) >= limit:
            break
        try:
            entry = json.loads(f.read_text())
            if not entry.get("hot_load"):
                continue
            entries.append({
                "id": entry.get("id", f.stem),
                "content": entry.get("content", ""),
                "tags": entry.get("tags", []),
                "context": entry.get("context", ""),
                "created_at": entry.get("created_at", ""),
                "hot_load": True,
            })
        except Exception:
            pass

    return {"results": entries, "count": len(entries)}


# ── LED-1165 Phase 2 #5 PR-B: MEMORY.md projection writer ──────────────

# Default target — Claude Code's auto-memory MEMORY.md for the current
# project (this is where Claude Code reads memory entries on session
# start). Override via env or function arg for testing / non-default
# project layouts.
DEFAULT_MEMORY_MD = Path.home() / ".claude" / "projects" / "-root" / "memory" / "MEMORY.md"

PROJECTION_START_MARKER = "<!-- delimit:start -->"
PROJECTION_END_MARKER = "<!-- delimit:end -->"


def _format_hot_entry_as_markdown(entry: Dict[str, Any]) -> str:
    """Render one delimit_memory entry as a Markdown bullet for the
    MEMORY.md hot-load index. Intentionally compact — the index loads
    into every Claude Code session, so each entry should fit on a
    line or two."""
    mid = entry.get("id", "?")
    content = (entry.get("content") or "").strip()
    # Single-line the content but cap at ~280 chars so the line stays
    # readable. Long content stays in delimit_memory; index is just
    # the hook for Claude to know it exists.
    one_line = " ".join(content.split())
    if len(one_line) > 280:
        one_line = one_line[:277] + "..."
    tags = entry.get("tags") or []
    tag_str = (" [tags: " + ", ".join(tags) + "]") if tags else ""
    ctx = (entry.get("context") or "").strip()
    ctx_line = f"\n  > {ctx[:200]}" if ctx else ""
    return f"- **{mid}**{tag_str} — {one_line}{ctx_line}"


def _render_managed_block(entries: List[Dict[str, Any]]) -> str:
    """Render the full managed section (between markers) for the
    MEMORY.md hot-load index. Includes a one-line preamble explaining
    the section so anyone editing the file understands what it is."""
    if not entries:
        body = (
            "_No hot-load memory entries. Add one with "
            "`delimit_memory_store(content=\"...\", hot_load=True)`._\n"
        )
    else:
        bullets = "\n".join(_format_hot_entry_as_markdown(e) for e in entries)
        body = bullets + "\n"

    # Brief header + body + caveat
    header = (
        "## Delimit hot memory (auto-projected from delimit_memory)\n"
        "\n"
        f"_Auto-managed by `delimit_memory_index` — projection of {len(entries)} "
        "entry/entries flagged `hot_load=True`. Edits inside this block are "
        "overwritten on next projection. Add an entry via "
        "`delimit_memory_store(content=\"...\", hot_load=True)`._\n"
        "\n"
    )
    return PROJECTION_START_MARKER + "\n" + header + body + PROJECTION_END_MARKER


def project_to_memory_md(
    target_path: Optional[Path] = None,
    dry_run: bool = False,
    limit: int = 200,
) -> Dict[str, Any]:
    """One-way projection: render hot_load=True entries from delimit_memory
    into a managed section of Claude Code's MEMORY.md.

    LED-1165 Phase 2 #5 PR-B. Composes PR-A's `list_hot` helper with a
    managed-section markdown writer. NEVER reads MEMORY.md back into
    delimit_memory — that's the explicit deliberation rule (Anthropic
    owns auto-memory's format; we don't risk drift).

    Behavior:
      - If target_path's file does NOT exist, create it with just the
        managed section (no other content).
      - If target_path's file exists and contains markers, replace the
        section between them. Content outside markers is preserved.
      - If target_path's file exists but has no markers, APPEND a new
        managed section to the end. Does NOT touch existing content.

    Args:
        target_path: where to write. Default DEFAULT_MEMORY_MD.
        dry_run: True returns the rendered content without writing.
        limit: cap on entries projected. Default 200 (matches list_hot).

    Returns:
        {
            "target": str,
            "dry_run": bool,
            "entries": int,
            "wrote_chars": int (or "would_write_chars"),
            "had_existing_block": bool,
            "had_existing_file": bool,
            "preserved_user_content": bool,
        }
    """
    if target_path is None:
        target_path = DEFAULT_MEMORY_MD

    target_path = Path(target_path)
    hot = list_hot(limit=limit)
    entries = hot.get("results", [])

    block = _render_managed_block(entries)

    had_existing_file = target_path.exists()
    had_existing_block = False
    preserved_user_content = False

    if had_existing_file:
        existing = target_path.read_text()
        start_idx = existing.find(PROJECTION_START_MARKER)
        end_idx = existing.find(PROJECTION_END_MARKER)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            had_existing_block = True
            preserved_user_content = bool(
                existing[:start_idx].strip() or existing[end_idx + len(PROJECTION_END_MARKER):].strip()
            )
            new_content = (
                existing[:start_idx]
                + block
                + existing[end_idx + len(PROJECTION_END_MARKER):]
            )
        else:
            # No markers — append to end, preserving everything above
            preserved_user_content = bool(existing.strip())
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            new_content = existing + sep + block + "\n"
    else:
        # Brand new file — just the managed section
        new_content = block + "\n"

    if not dry_run:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(new_content)

    return {
        "target": str(target_path),
        "dry_run": dry_run,
        "entries": len(entries),
        "wrote_chars" if not dry_run else "would_write_chars": len(new_content),
        "had_existing_block": had_existing_block,
        "had_existing_file": had_existing_file,
        "preserved_user_content": preserved_user_content,
    }
