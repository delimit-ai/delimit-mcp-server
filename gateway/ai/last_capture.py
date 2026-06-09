"""LED-1705 — deterministic session-end capture stamp.

A tiny, dependency-free helper that records the most recent successful
session-context capture to ``~/.delimit/.last_capture`` (env-aware via
``DELIMIT_HOME`` / ``DELIMIT_NAMESPACE_ROOT``).

The stamp lets three independent capture paths coordinate without clobbering
each other's richer artifacts:

  * model-invoked capture (``capture_soul`` / ``session_handoff``) writes
    ``source="model"`` — the richest artifact.
  * the Claude Code Stop hook writes ``source="deterministic"`` ONLY when no
    fresh model capture exists (freshness gate, default 5 min).
  * ``revive`` salvages an orphaned transcript (crash / SIGKILL path) when the
    previous session left no stamp at all.

Everything here is CHEAP and best-effort: no LLM calls, no network, failures
never raise into the caller.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# Freshness window: a model capture newer than this suppresses the
# deterministic floor so we never clobber the richer artifact.
FRESH_CAPTURE_SECONDS = 5 * 60


def _delimit_home() -> Path:
    """Env-aware ~/.delimit (mirrors ledger_manager._delimit_home)."""
    for env_key in ("DELIMIT_HOME", "DELIMIT_NAMESPACE_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val)
    return Path.home() / ".delimit"


def last_capture_path() -> Path:
    """Absolute path to the ``.last_capture`` stamp file."""
    return _delimit_home() / ".last_capture"


def stamp_capture(
    source: str,
    session_id: str = "",
    quality: str = "",
    ts: Optional[float] = None,
) -> Optional[Path]:
    """Write the ``.last_capture`` stamp after a successful capture.

    Best-effort: returns the path on success, ``None`` on any failure. Never
    raises — a capture must not fail because the stamp couldn't be written.

    Args:
        source: "model" | "deterministic" — who produced the capture.
        session_id: optional id of the captured soul / handoff.
        quality: optional grade, e.g. "floor" for deterministic captures.
        ts: optional epoch seconds; defaults to now.
    """
    try:
        when = float(ts) if ts is not None else time.time()
        payload: Dict[str, Any] = {
            "ts": when,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
            "session_id": session_id or "",
            "source": source,
        }
        if quality:
            payload["quality"] = quality
        path = last_capture_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path
    except Exception:
        return None


def read_last_capture() -> Optional[Dict[str, Any]]:
    """Read and parse the ``.last_capture`` stamp, or ``None`` if absent/bad."""
    try:
        path = last_capture_path()
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


def has_fresh_model_capture(
    within_seconds: int = FRESH_CAPTURE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """True iff a ``source="model"`` capture exists within ``within_seconds``.

    Used by the Stop hook's deterministic floor (skip when a fresh, richer
    model capture already exists) and exposed here for unit testing.
    """
    stamp = read_last_capture()
    if not stamp or stamp.get("source") != "model":
        return False
    try:
        ts = float(stamp.get("ts", 0))
    except (TypeError, ValueError):
        return False
    cur = float(now) if now is not None else time.time()
    return (cur - ts) <= within_seconds


def parse_transcript_tail(
    transcript_path: str,
    max_turns: int = 10,
) -> Dict[str, Any]:
    """Parse the tail of a Claude Code transcript (JSONL) cheaply.

    Returns a small dict with the last assistant text and the names of tool
    calls seen in the tail — enough to seed a deterministic floor handoff.
    No LLM call; pure JSONL parsing. Best-effort: returns empty fields on any
    error so callers never have to guard.

    Robust to "thinking-tails": when a session ends mid-work the last few
    transcript lines are often ``tool_use`` + ``thinking`` blocks with NO
    ``text`` block. We therefore (a) prefer the last assistant ``text`` block,
    (b) fall back to the last assistant ``thinking`` block (prefixed
    ``[thinking] ``) so ``final_assistant_text`` is never empty when assistant
    turns exist, and (c) widen the scan beyond ``max_turns`` (capped) to recover
    a real ``text`` block pushed out of the immediate tail by a tool/thinking
    run. ``tool_calls`` is still extracted from the immediate ``max_turns`` tail.

    Args:
        transcript_path: path to the transcript JSONL file.
        max_turns: how many trailing transcript lines to consider for
            ``tool_calls`` and ``turns``. The text/thinking scan may look back
            further (capped) to recover a real ``text`` block.

    Returns:
        {"final_assistant_text": str, "tool_calls": [str, ...], "turns": int}
    """
    # Cap on how far back we scan for a real text block when the immediate
    # tail has none. Cheap: a bounded slice, no LLM, no extra IO.
    SCAN_CAP = 40

    result: Dict[str, Any] = {
        "final_assistant_text": "",
        "tool_calls": [],
        "turns": 0,
    }

    def _extract(content: Any, tool_sink: Optional[List[str]]) -> Dict[str, str]:
        """Pull text/thinking out of one message's content blocks.

        Appends tool_use names to ``tool_sink`` when provided. Returns the
        joined text and thinking for this message (either may be empty).
        """
        text_parts: List[str] = []
        think_parts: List[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    if tool_sink is not None:
                        name = block.get("name")
                        if name:
                            tool_sink.append(str(name))
                elif btype == "text":
                    t = block.get("text")
                    if t:
                        text_parts.append(str(t))
                elif btype == "thinking":
                    # thinking blocks carry their text under "thinking", not "text".
                    th = block.get("thinking")
                    if th:
                        think_parts.append(str(th))
        elif isinstance(content, str):
            text_parts.append(content)
        return {
            "text": "\n".join(text_parts).strip(),
            "thinking": "\n".join(think_parts).strip(),
        }

    def _role_content(obj: Any):
        """Normalize the (role, content) pair from a transcript line."""
        msg = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(msg, dict):
            role = msg.get("role", "") or (obj.get("type", "") if isinstance(obj, dict) else "")
            content = msg.get("content")
        else:
            role = obj.get("type", "") if isinstance(obj, dict) else ""
            content = obj.get("content") if isinstance(obj, dict) else None
        return role, content

    try:
        if not transcript_path:
            return result
        p = Path(transcript_path)
        if not p.exists():
            return result
        lines = [l for l in p.read_text(errors="replace").splitlines() if l.strip()]
        tail = lines[-max_turns:] if max_turns > 0 else lines
        result["turns"] = len(tail)

        tool_calls: List[str] = []
        final_text = ""
        final_thinking = ""

        # Pass 1: the immediate tail — tool_calls (authoritative here) + the
        # last text/thinking seen within the tail.
        for raw in tail:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            role, content = _role_content(obj)
            parts = _extract(content, tool_calls)
            if role == "assistant":
                if parts["text"]:
                    final_text = parts["text"]
                if parts["thinking"]:
                    final_thinking = parts["thinking"]

        # Pass 2 (widen): if the immediate tail had no real text block, look
        # back further (capped) to recover the last assistant text block that
        # a tool/thinking run pushed out of the window. Tool calls are NOT
        # re-collected here — they stay scoped to the immediate tail.
        if not final_text and (max_turns <= 0 or len(lines) > len(tail)):
            wide = lines[-SCAN_CAP:] if SCAN_CAP > 0 else lines
            for raw in wide:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                role, content = _role_content(obj)
                if role != "assistant":
                    continue
                parts = _extract(content, None)
                if parts["text"]:
                    final_text = parts["text"]
                # keep tracking thinking too so the fallback uses the latest.
                if parts["thinking"]:
                    final_thinking = parts["thinking"]

        # Prefer real text; fall back to the last thinking block so the field
        # is never empty when assistant turns exist.
        if not final_text and final_thinking:
            final_text = "[thinking] " + final_thinking

        result["tool_calls"] = tool_calls
        result["final_assistant_text"] = final_text
        return result
    except Exception:
        return result
