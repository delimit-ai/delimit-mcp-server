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

# Legacy CLI store filename. The npm CLI historically wrote memories as
# newline-delimited JSON (`memories.jsonl`) using a `text`/`created`/`source`
# schema, while the MCP store writes one `mem-*.json` file per entry using
# `content`/`created_at`/`context`. The readers below reconcile both so a
# customer who created memories via the old CLI still sees them through the
# MCP tools (FIX C — non-destructive; the .jsonl is never rewritten here).
LEGACY_JSONL_NAME = "memories.jsonl"


def _ensure_dir():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _tokenize(query: str) -> List[str]:
    """Split a search query into lowercased whitespace-delimited tokens.

    Used by search() for OR-semantics keyword matching: an entry is a hit
    if it contains at least one token. Empty / whitespace-only queries
    yield no tokens (callers preserve their own empty-query behavior).
    """
    return [t for t in (query or "").lower().split() if t]


def _normalize_legacy_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a legacy `memories.jsonl` record to the MCP entry shape.

    Legacy CLI schema: {id, text, tags, created, source}
    MCP schema:        {id, content, tags, context, created_at, hot_load}

    Maps text->content and created->created_at without dropping the
    original keys, and synthesizes a context from `source` when absent so
    downstream readers behave uniformly. Mirrors the CLI's readMemories
    normalization (npm-delimit/bin/delimit-cli.js) for cross-tool parity.
    """
    entry = dict(raw)
    if entry.get("text") and not entry.get("content"):
        entry["content"] = entry["text"]
    if entry.get("content") and not entry.get("text"):
        entry["text"] = entry["content"]
    if entry.get("created") and not entry.get("created_at"):
        entry["created_at"] = entry["created"]
    if entry.get("created_at") and not entry.get("created"):
        entry["created"] = entry["created_at"]
    if not entry.get("context") and entry.get("source"):
        entry["context"] = entry["source"]
    return entry


def _read_legacy_jsonl() -> List[Dict[str, Any]]:
    """Read and normalize legacy `memories.jsonl` entries, if present.

    Defensive by contract: a missing or malformed file yields an empty
    list and never raises. Malformed individual lines are skipped so one
    bad line does not lose the rest of the file.
    """
    path = MEMORY_DIR / LEGACY_JSONL_NAME
    entries: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return entries
        text = path.read_text()
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(raw, dict):
            entries.append(_normalize_legacy_entry(raw))
    return entries


def _load_all_entries() -> List[Dict[str, Any]]:
    """Load every memory entry from both stores, deduped by id.

    Reads the per-entry `mem-*.json` files (MCP, primary) and the legacy
    `memories.jsonl` (CLI, backwards-compat). On an id collision the
    `mem-*.json` entry wins — it is the authoritative MCP store and may
    carry fields (e.g. hot_load) the legacy record lacks. Entries are
    returned newest-first by created_at so callers that slice keep the
    most recent. Fully defensive: unreadable files are skipped.

    FIX C: the legacy `memories.jsonl` is read-only here — never deleted
    or rewritten — preserving a customer's existing CLI-authored memories.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def _add(entry: Dict[str, Any], key: str, *, overwrite: bool) -> None:
        if key not in by_id:
            by_id[key] = entry
            order.append(key)
        elif overwrite:
            by_id[key] = entry

    # Primary store: mem-*.json (authoritative, wins on conflict).
    for f in MEMORY_DIR.glob("*.json"):
        try:
            entry = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        entry.setdefault("id", f.stem)
        _add(entry, entry.get("id") or f.stem, overwrite=True)

    # Legacy jsonl: only fills ids the primary store does not already have.
    for entry in _read_legacy_jsonl():
        key = entry.get("id")
        if not key:
            # No id to dedupe on — keep it, it cannot collide.
            order.append(id(entry))  # unique sentinel key
            by_id[id(entry)] = entry
            continue
        _add(entry, key, overwrite=False)

    entries = [by_id[k] for k in order]
    entries.sort(key=lambda e: e.get("created_at") or e.get("created") or "", reverse=True)
    return entries


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
    """Search memories by keyword matching.

    FIX A: the query is tokenized on whitespace and matched with OR
    semantics — an entry is a hit if it contains at least one token in its
    content, tags, or context. Previously the entire query had to appear as
    one contiguous substring, so any multi-word query returned zero hits.

    Results are ranked by the number of distinct query tokens matched
    (descending), tie-broken by recency (created_at descending). The
    `relevance` field is preserved in the return schema and now carries the
    matched-token count, the primary ranking signal.

    An empty (or whitespace-only) query preserves the previous behavior of
    returning no results.

    FIX C: reads both the per-entry `mem-*.json` MCP store and the legacy
    `memories.jsonl` CLI store (deduped, MCP wins on id conflict).
    """
    _ensure_dir()
    tokens = _tokenize(query)
    results = []

    # Empty / whitespace-only query: preserve prior behavior (no hits).
    if not tokens:
        return {"query": query, "results": results, "count": 0}

    for entry in _load_all_entries():
        content = (entry.get("content") or "").lower()
        tags = " ".join(entry.get("tags") or []).lower()
        context = (entry.get("context") or "").lower()
        haystacks = (content, tags, context)

        matched_tokens = 0
        total_occurrences = 0
        for tok in tokens:
            hit = False
            for hay in haystacks:
                c = hay.count(tok)
                if c:
                    hit = True
                    total_occurrences += c
            if hit:
                matched_tokens += 1

        if matched_tokens >= 1:
            results.append({
                "id": entry.get("id", ""),
                "content": (entry.get("content") or "")[:500],
                "tags": entry.get("tags") or [],
                "created_at": entry.get("created_at") or entry.get("created") or "",
                # `relevance` preserved in schema; now = matched-token count
                # (primary ranking signal). _occurrences is an internal
                # tie-break aid, dropped before return.
                "relevance": matched_tokens,
                "_occurrences": total_occurrences,
            })

    # Rank: most tokens matched first, then most occurrences, then recency.
    results.sort(
        key=lambda r: (r["relevance"], r["_occurrences"], r.get("created_at") or ""),
        reverse=True,
    )
    for r in results:
        r.pop("_occurrences", None)

    results = results[:limit]
    return {"query": query, "results": results, "count": len(results)}


def get_recent(limit: int = 5) -> Dict[str, Any]:
    """Get recent memory entries.

    FIX C: reads both the per-entry `mem-*.json` MCP store and the legacy
    `memories.jsonl` CLI store. Entries are deduped by id (MCP wins) and
    ordered newest-first by created_at (legacy `created` is normalized to
    `created_at`). Legacy entries surface `hot_load=False` since the field
    pre-dates that schema.
    """
    _ensure_dir()
    entries = []

    for entry in _load_all_entries():
        if len(entries) >= limit:
            break
        entries.append({
            "id": entry.get("id", ""),
            "content": (entry.get("content") or "")[:500],
            "tags": entry.get("tags") or [],
            "created_at": entry.get("created_at") or entry.get("created") or "",
            "hot_load": bool(entry.get("hot_load", False)),
        })

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

    for entry in _load_all_entries():
        if len(entries) >= limit:
            break
        if not entry.get("hot_load"):
            continue
        entries.append({
            "id": entry.get("id", ""),
            "content": entry.get("content") or "",
            "tags": entry.get("tags") or [],
            "context": entry.get("context") or "",
            "created_at": entry.get("created_at") or entry.get("created") or "",
            "hot_load": True,
        })

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
