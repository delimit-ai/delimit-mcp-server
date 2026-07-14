"""
Toolcard Delta Cache — LED-219

MCP servers dump full tool definitions every session. GitHub's MCP server
alone sends 40K+ tokens of tool schemas. This module stores hashed tool
schemas and only surfaces diffs when schemas change, cutting token waste
on tool definitions dramatically.

This is a MEASUREMENT tool first — it shows the savings potential. The
actual MCP protocol optimization to send compressed schemas is a separate
step.

Architecture:
  - SHA256 hash of each tool's canonical schema (name + description + parameters)
  - Persistent JSON cache at ~/.delimit/toolcard_cache.json
  - Per-session JSONL logs at ~/.delimit/toolcard_sessions/{date}.jsonl
  - Thread-safe via atomic writes (write to tmp, rename)
  - No external dependencies — stdlib only
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.toolcard_cache")

CACHE_FILE = Path.home() / ".delimit" / "toolcard_cache.json"
SESSION_DIR = Path.home() / ".delimit" / "toolcard_sessions"


def _canonical_json(obj: Any) -> str:
    """Produce a deterministic JSON string for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_schema(tool: Dict[str, Any]) -> str:
    """SHA256 hash of a tool's canonical schema (name + description + parameters)."""
    canonical = {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("parameters", {}),
    }
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def _estimate_tokens(obj: Any) -> int:
    """Estimate token count: len(JSON) / 4 (standard approximation)."""
    return max(1, len(_canonical_json(obj)) // 4)


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".toolcard_"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ToolcardCache:
    """Hashed tool schema registry. Sends full schemas on first session, diffs after."""

    def __init__(self, cache_file: Optional[Path] = None, session_dir: Optional[Path] = None):
        self._cache_file = cache_file or CACHE_FILE
        self._session_dir = session_dir or SESSION_DIR
        self.cache: Dict[str, Dict[str, Any]] = self._load()
        # Per-session tracking
        self._session_start = datetime.now(timezone.utc).isoformat()
        self._session_calls: Dict[str, int] = {}  # tool_name -> call count
        self._session_registered = 0
        self._session_hits = 0
        self._session_misses = 0

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load cache from disk. Returns empty dict if missing or corrupt."""
        try:
            if self._cache_file.exists():
                with open(self._cache_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Toolcard cache load failed: %s", e)
        return {}

    def _save(self) -> None:
        """Persist cache to disk atomically."""
        _atomic_write_json(self._cache_file, self.cache)

    def register_tools(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Register tool schemas. Returns only NEW or CHANGED tools.

        Args:
            tools: List of tool schema dicts, each with 'name', 'description', 'parameters'.

        Returns:
            Dict with:
                new_tools: list of tool schemas not previously cached
                changed_tools: list of tool schemas whose hash differs
                unchanged_tools: list of tool names (no schema, just names)
                full_tokens: estimated tokens if all schemas were sent
                delta_tokens: actual tokens for just new/changed
                savings_pct: percentage reduction
                saved_tokens: absolute token count saved
        """
        new_tools = []
        changed_tools = []
        unchanged_names = []
        now = datetime.now(timezone.utc).isoformat()

        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            h = _hash_schema(tool)

            if name not in self.cache:
                # New tool
                new_tools.append(tool)
                self.cache[name] = {
                    "hash": h,
                    "schema": tool,
                    "first_seen": now,
                    "last_changed": now,
                }
                self._session_misses += 1
            elif self.cache[name]["hash"] != h:
                # Changed tool
                changed_tools.append(tool)
                self.cache[name] = {
                    "hash": h,
                    "schema": tool,
                    "first_seen": self.cache[name].get("first_seen", now),
                    "last_changed": now,
                }
                self._session_misses += 1
            else:
                # Unchanged — cache hit
                unchanged_names.append(name)
                self._session_hits += 1

        self._session_registered = len(tools)
        self._save()

        # Token calculations
        full_tokens = sum(_estimate_tokens(t) for t in tools)
        delta_schemas = new_tools + changed_tools
        delta_tokens = sum(_estimate_tokens(t) for t in delta_schemas)
        # Unchanged tools still need their names sent (compact summary)
        delta_tokens += sum(len(n) // 4 + 1 for n in unchanged_names)

        saved_tokens = max(0, full_tokens - delta_tokens)
        savings_pct = round((saved_tokens / full_tokens * 100), 1) if full_tokens > 0 else 0.0

        return {
            "new_tools": new_tools,
            "changed_tools": changed_tools,
            "unchanged_tools": unchanged_names,
            "full_tokens": full_tokens,
            "delta_tokens": delta_tokens,
            "savings_pct": savings_pct,
            "saved_tokens": saved_tokens,
            "total_registered": len(tools),
            "cache_size": len(self.cache),
        }

    def get_delta(self, tool_names: List[str]) -> Dict[str, Any]:
        """Return only schemas that changed since last check.

        Args:
            tool_names: List of tool names to check against the cache.

        Returns:
            Dict with cached (hit) and missing (miss) tools.
        """
        cached = []
        missing = []

        for name in tool_names:
            if name in self.cache:
                cached.append(name)
                self._session_hits += 1
            else:
                missing.append(name)
                self._session_misses += 1

        return {
            "cached": cached,
            "missing": missing,
            "cached_count": len(cached),
            "missing_count": len(missing),
            "hit_rate": round(len(cached) / len(tool_names) * 100, 1) if tool_names else 0.0,
        }

    def record_call(
        self,
        tool_name: str,
        outcome: Optional[str] = None,
        duration_ms: Optional[float] = None,
        linked_id: Optional[str] = None,
    ) -> None:
        """Record that a tool was called: in-memory (session) AND durably.

        The prior implementation only kept an in-memory counter that vanished on
        process restart, so tool utilization was never observable across sessions
        (usage.json stayed empty and dormancy was unmeasurable). We now also
        append to a crash-safe, append-only JSONL event log so utilization is
        measurable over time. Append is O(1) with no read-modify-write race.
        Analytics must never break a tool call, so all failures are swallowed.

        Org-instrumentation (doc 25 §8 Phase 1 item #5 / gap G5): the optional
        ``outcome`` / ``duration_ms`` / ``linked_id`` fields let §9's 30-day
        review read REAL data (ok/error/refused/blocked counts, latency, and
        the ledger/task id an action was linked to) instead of vibes. All three
        are OPTIONAL and are written only when supplied, so:
          * existing callers that pass only ``tool_name`` are unchanged, and
          * old records already on customer machines (no outcome key) still
            parse — :meth:`usage_summary` treats a missing outcome as untagged.
        ``outcome`` is normalised to a lowercase token; anything unrecognised is
        recorded verbatim so we never silently drop a caller's signal.
        """
        self._session_calls[tool_name] = self._session_calls.get(tool_name, 0) + 1
        try:
            usage_log = self._cache_file.parent / "tool_usage.jsonl"
            usage_log.parent.mkdir(parents=True, exist_ok=True)
            rec: Dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": tool_name,
            }
            if outcome is not None:
                rec["outcome"] = str(outcome).strip().lower()
            if duration_ms is not None:
                try:
                    rec["duration_ms"] = round(float(duration_ms), 3)
                except (TypeError, ValueError):
                    pass
            if linked_id:
                rec["linked_id"] = str(linked_id)
            with open(usage_log, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def usage_summary(self, registry: Optional[List[str]] = None) -> Dict[str, Any]:
        """Aggregate the durable usage log into per-tool counts + dormancy.

        Reads tool_usage.jsonl (written by record_call). When `registry` (the
        full list of registered tool names) is provided, tools that appear in
        the registry but never in the log are reported as `dormant`.
        """
        usage_log = self._cache_file.parent / "tool_usage.jsonl"
        counts: Dict[str, int] = {}
        last_seen: Dict[str, str] = {}
        total = 0
        # Org-instrumentation aggregates (doc 25 §8 item #5). Records that
        # predate the outcome field simply don't contribute — backward
        # compatible by construction.
        outcomes: Dict[str, int] = {}
        outcomes_by_tool: Dict[str, Dict[str, int]] = {}
        tagged = 0
        duration_total = 0.0
        duration_n = 0
        try:
            with open(usage_log, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    name = rec.get("tool")
                    if not name:
                        continue
                    counts[name] = counts.get(name, 0) + 1
                    ts = rec.get("ts")
                    if ts and (name not in last_seen or ts > last_seen[name]):
                        last_seen[name] = ts
                    total += 1
                    oc = rec.get("outcome")
                    if oc:
                        oc = str(oc)
                        outcomes[oc] = outcomes.get(oc, 0) + 1
                        outcomes_by_tool.setdefault(name, {})
                        outcomes_by_tool[name][oc] = (
                            outcomes_by_tool[name].get(oc, 0) + 1
                        )
                        tagged += 1
                    dur = rec.get("duration_ms")
                    if isinstance(dur, (int, float)):
                        duration_total += float(dur)
                        duration_n += 1
        except FileNotFoundError:
            pass
        result: Dict[str, Any] = {
            "total_calls": total,
            "distinct_tools_used": len(counts),
            "counts": dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)),
            "last_seen": last_seen,
            "usage_log": str(usage_log),
            # Outcome instrumentation — empty dicts when no record is tagged.
            "outcomes": dict(sorted(outcomes.items(), key=lambda x: x[1], reverse=True)),
            "outcomes_by_tool": outcomes_by_tool,
            "tagged_calls": tagged,
            "avg_duration_ms": round(duration_total / duration_n, 3) if duration_n else None,
            "timed_calls": duration_n,
        }
        if registry is not None:
            used = set(counts)
            result["registry_size"] = len(registry)
            result["dormant"] = sorted(t for t in registry if t not in used)
            result["dormant_count"] = len(result["dormant"])
        return result

    def get_stats(self) -> Dict[str, Any]:
        """Return cache stats: total tools, cached, cache hit rate, token savings."""
        total_checks = self._session_hits + self._session_misses
        hit_rate = round(
            (self._session_hits / total_checks * 100), 1
        ) if total_checks > 0 else 0.0

        # Estimate total cached schema tokens
        cached_tokens = sum(
            _estimate_tokens(entry.get("schema", {}))
            for entry in self.cache.values()
        )

        # Most called tools this session
        top_tools = sorted(
            self._session_calls.items(), key=lambda x: x[1], reverse=True
        )[:10]

        return {
            "total_cached_tools": len(self.cache),
            "session_registered": self._session_registered,
            "session_hits": self._session_hits,
            "session_misses": self._session_misses,
            "session_hit_rate": hit_rate,
            "cached_schema_tokens": cached_tokens,
            "session_tools_called": dict(top_tools),
            "session_start": self._session_start,
            "cache_file": str(self._cache_file),
        }

    def estimate_savings(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Estimate token savings without modifying the cache.

        Dry-run version of register_tools — shows what WOULD be saved.
        """
        hits = 0
        misses = 0

        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            h = _hash_schema(tool)
            if name in self.cache and self.cache[name]["hash"] == h:
                hits += 1
            else:
                misses += 1

        full_tokens = sum(_estimate_tokens(t) for t in tools)
        # If all cached, only names need sending
        cached_names_tokens = sum(len(t.get("name", "")) // 4 + 1 for t in tools if t.get("name") in self.cache and self.cache.get(t.get("name"), {}).get("hash") == _hash_schema(t))
        missed_tokens = sum(
            _estimate_tokens(t) for t in tools
            if t.get("name") not in self.cache or self.cache.get(t.get("name"), {}).get("hash") != _hash_schema(t)
        )
        delta_tokens = cached_names_tokens + missed_tokens
        saved_tokens = max(0, full_tokens - delta_tokens)
        savings_pct = round((saved_tokens / full_tokens * 100), 1) if full_tokens > 0 else 0.0

        return {
            "total_tools": len(tools),
            "would_be_cached": hits,
            "would_need_sending": misses,
            "full_tokens": full_tokens,
            "delta_tokens": delta_tokens,
            "savings_pct": savings_pct,
            "saved_tokens": saved_tokens,
        }

    def clear(self) -> Dict[str, Any]:
        """Clear the cache. Forces full schema send next session."""
        count = len(self.cache)
        self.cache = {}
        self._save()
        return {
            "cleared": count,
            "message": f"Cleared {count} cached tool schemas. Next session will send full schemas.",
        }

    def flush_session(self) -> Dict[str, Any]:
        """Write session stats to the per-session JSONL log."""
        self._session_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_file = self._session_dir / f"{date_str}.jsonl"

        record = {
            "session_start": self._session_start,
            "flushed_at": datetime.now(timezone.utc).isoformat(),
            "stats": self.get_stats(),
        }

        with open(session_file, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

        return {"written_to": str(session_file), "record": record}


# Module-level singleton for use by server.py
_cache_instance: Optional[ToolcardCache] = None


def get_cache() -> ToolcardCache:
    """Get or create the module-level cache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = ToolcardCache()
    return _cache_instance


def reset_cache() -> None:
    """Reset the singleton (for testing)."""
    global _cache_instance
    _cache_instance = None
