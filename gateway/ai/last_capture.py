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
import re
import stat
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
    **metadata: Any,
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
        # Additive provenance fields used by the portfolio-level continuity
        # floor.  Keep the legacy four-field shape when callers do not supply
        # metadata, and never let arbitrary values replace the coordinator's
        # authoritative fields above.
        for key in (
            "project_path",
            "venture",
            "transcript_path",
            "transcript_id",
            "transcript_size",
            "transcript_mtime_ns",
            "transcript_tail_sha256",
            "logical_session_id",
            "capture_key",
            "handoff_id",
            "launcher_run_id",
        ):
            value = metadata.get(key)
            if key in {"transcript_size", "transcript_mtime_ns"}:
                if value is not None:
                    payload[key] = int(value)
            elif value:
                payload[key] = str(value)
        path = last_capture_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, payload)
        return path
    except Exception:
        return None


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically replace a small JSON pointer without following symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise OSError(f"refusing symlink pointer: {path}")
    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as fh:
            os.chmod(tmp, 0o600)
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_last_capture() -> Optional[Dict[str, Any]]:
    """Read and parse the ``.last_capture`` stamp, or ``None`` if absent/bad."""
    try:
        path = last_capture_path()
        if not path.is_file() or path.is_symlink():
            return None
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
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
    # Read only the trailing window of the transcript: transcripts can be many
    # MB and this runs in time-boxed paths (Stop hook + SessionStart reconcile).
    # 64KB comfortably holds the last ~40 lines we ever scan (SCAN_CAP) and
    # avoids loading a multi-MB file just to read its tail.
    TAIL_BYTES = 65536

    result: Dict[str, Any] = {
        "final_assistant_text": "",
        "tool_calls": [],
        "turns": 0,
    }

    def _read_tail(path: Path) -> str:
        """Read only the trailing ~TAIL_BYTES of the file (seek-from-end).

        If the read started mid-file (file larger than the window), the first
        line is a fragment and is dropped. Best-effort: on ANY failure falls
        back to a full ``read_text`` so correctness never regresses.
        """
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - TAIL_BYTES)
                fh.seek(start)
                chunk = fh.read()
            text = chunk.decode("utf-8", errors="replace")
            if start > 0:
                # Drop the leading partial line — it begins mid-record.
                nl = text.find("\n")
                text = text[nl + 1:] if nl != -1 else ""
            return text
        except Exception:
            # Fall back to the whole-file read; never regress correctness.
            return path.read_text(errors="replace")

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
        lines = [line for line in _read_tail(p).splitlines() if line.strip()]
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


# LED-4057: the old ten-line summary above remains intentionally small and
# backwards compatible for callers that only need a Stop-hook floor.  The
# quota/portfolio path needs structured evidence, logical-session identity,
# task notifications, and a substantive visible decision.  Keep that richer
# parser separate so no private ``thinking`` block can accidentally enter the
# durable continuity artifact.
_RICH_TAIL_BYTES = 8 * 1024 * 1024
_RICH_RECORD_CAP = 1200
_VISIBLE_TEXT_CAP = 16 * 1024
_TASK_OUTPUT_CAP = 64 * 1024
_REF_RE = re.compile(r"\b(?:LED|STR)-\d+\b|(?i:(?:\bPR\s*)?#\d+\b)")
_QUOTA_TEXT_RE = re.compile(
    r"(?:hit your (?:monthly|weekly) (?:spend )?limit|"
    r"raise it at claude\.ai/settings|usage\?from=cc_cli_limit_message)",
    re.IGNORECASE,
)
_TRANSITION_RE = re.compile(
    r"^(?:let me|i(?:'ll| will)|while .* settles|checking|one moment)\b",
    re.IGNORECASE,
)
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>(?P<body>.*?)</task-notification>", re.DOTALL
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:ghp|github_pat|sk|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
    re.compile(
        r"(?i)\b(authorization\s*[:=]\s*(?:bearer|token)\s+)"
        r"[-A-Za-z0-9._~+/=]{8,}"
    ),
    re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)['\"]?[-A-Za-z0-9._~+/=]{8,}"),
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _redact_continuity_text(text: str) -> str:
    """Best-effort secret redaction before transcript text becomes durable."""
    value = text or ""
    try:
        try:
            from ai.pii_redact import redact
        except ImportError:  # pragma: no cover
            from pii_redact import redact  # type: ignore
        result = redact(value)
        # Deliberately discard token_map: continuity never needs reconstruction.
        value = str(result.get("redacted") or "")
    except Exception:
        pass
    value = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _SECRET_PATTERNS[0].sub("[REDACTED]", value)
    value = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", value)
    value = _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", value)
    value = _SECRET_PATTERNS[3].sub(r"\1[REDACTED]@", value)
    return value


def _read_jsonl_tail_records(
    transcript_path: str,
    byte_cap: int = _RICH_TAIL_BYTES,
    record_cap: int = _RICH_RECORD_CAP,
) -> List[Dict[str, Any]]:
    """Read a bounded JSONL tail, dropping a leading partial record."""
    try:
        p = Path(transcript_path)
        if not p.is_file() or p.is_symlink():
            return []
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max(1, byte_cap))
            fh.seek(start)
            raw = fh.read(max(1, byte_cap))
        text = raw.decode("utf-8", errors="replace")
        if start:
            newline = text.find("\n")
            text = text[newline + 1:] if newline >= 0 else ""
        records: List[Dict[str, Any]] = []
        for line in text.splitlines()[-record_cap:]:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                records.append(obj)
        return records
    except Exception:
        return []


def _message_role_content(obj: Dict[str, Any]) -> tuple[str, Any]:
    message = obj.get("message")
    if isinstance(message, dict):
        return str(message.get("role") or obj.get("type") or ""), message.get("content")
    return str(obj.get("type") or ""), obj.get("content")


def _visible_text_blocks(content: Any) -> List[str]:
    """Return only user-visible text; deliberately excludes thinking blocks."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def _is_substantive_visible_text(text: str) -> bool:
    compact = text.strip()
    if not compact or _QUOTA_TEXT_RE.search(compact):
        return False
    # A short narration such as "Let me check CI" is not the session decision.
    if len(compact) < 180 and _TRANSITION_RE.search(compact):
        return False
    return len(compact) >= 80 or (
        "recommendation" in compact.lower()
        or bool(re.search(r"(?m)^\s*(?:1[.)]|##\s)", compact))
    )


def _xml_value(body: str, tag: str) -> str:
    match = re.search(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", body, re.DOTALL
    )
    return match.group(1).strip() if match else ""


def _validated_task_output(path_text: str, task_id: str) -> Optional[Path]:
    """Validate a Claude task output path without resolving through symlinks.

    Accepted shape:
      /tmp/claude-*/<encoded-project>/<session>/tasks/<task-id>.output
    Tests and alternate installations may add equally-shaped roots through
    ``DELIMIT_CLAUDE_TASK_ROOTS``.
    """
    if not path_text or not task_id or not path_text.startswith("/"):
        return None
    try:
        candidate = Path(path_text)
        if candidate.name != f"{task_id}.output":
            return None
        if candidate.parent.name != "tasks":
            return None
        allowed_parents: List[Path] = []
        parts = candidate.parts
        # /tmp/claude-X/<encoded>/<session>/tasks/<id>.output
        if (
            len(parts) >= 7
            and parts[1] == "tmp"
            and parts[2].startswith("claude-")
        ):
            allowed_parents.append(Path(*parts[:6]))
        for raw_root in os.environ.get("DELIMIT_CLAUDE_TASK_ROOTS", "").split(
            os.pathsep
        ):
            raw_root = raw_root.strip()
            if raw_root and raw_root.startswith("/"):
                allowed_parents.append(Path(raw_root))
        if not any(candidate.parent == root for root in allowed_parents):
            return None
        # Reject every symlink in the existing chain.  ``resolve`` alone would
        # make a malicious escape look canonical.
        cursor = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                return None
        if not candidate.exists():
            return candidate
        if not candidate.is_file() or candidate.is_symlink():
            return None
        return candidate
    except Exception:
        return None


def _bounded_task_output(path: Optional[Path]) -> str:
    if path is None or not path.exists():
        return ""
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ""
        raw = os.read(fd, _TASK_OUTPUT_CAP + 1)
        if len(raw) > _TASK_OUTPUT_CAP:
            raw = raw[:_TASK_OUTPUT_CAP]
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_transcript_context(transcript_path: str) -> Dict[str, Any]:
    """Parse event-bound, public continuity facts from a Claude JSONL tail.

    The result is intentionally evidence-shaped.  It retains structured tool
    inputs/results for project resolution but never copies assistant thinking.
    """
    empty: Dict[str, Any] = {
        "final_assistant_text": "",
        "tool_calls": [],
        "turns": 0,
        "records": [],
        "logical_session_id": "",
        "chat_session_id": "",
        "transcript_id": "",
        "references": [],
        "background_tasks": [],
    }
    records = _read_jsonl_tail_records(transcript_path)
    if not records:
        return empty

    # Claude currently uses snake-case ``session_id`` for the inner logical
    # run and camel-case ``sessionId`` for the durable chat.  A resumed inner
    # run in one JSONL must get a distinct floor.
    logical_id = ""
    chat_id = ""
    for obj in records:
        inner = obj.get("session_id")
        outer = obj.get("sessionId")
        if isinstance(inner, str) and inner:
            logical_id = inner
        if isinstance(outer, str) and outer:
            chat_id = outer
    if logical_id:
        # Queue/task records often omit ``session_id``.  Anchor at the FIRST
        # record carrying the final inner id, then include later no-id records;
        # otherwise notifications from an earlier inner run leak into this
        # floor merely because they share the outer JSONL.
        start = next(
            (
                index
                for index, obj in enumerate(records)
                if obj.get("session_id") == logical_id
            ),
            0,
        )
        scoped = [
            obj for obj in records[start:]
            if not obj.get("session_id") or obj.get("session_id") == logical_id
        ]
    else:
        scoped = records

    tool_calls: List[str] = []
    visible: List[str] = []
    task_by_id: Dict[str, Dict[str, Any]] = {}
    background_tool_ids: set[str] = set()
    # The durable decision may be farther back, but project/task events must be
    # bound to the dying work edge, not every notification in a long inner run.
    event_records = scoped[-40:]

    def remember_task(task_id: str, **values: Any) -> None:
        if not task_id:
            return
        current = task_by_id.setdefault(
            task_id,
            {
                "task_id": task_id,
                "tool_use_id": "",
                "status": "pending",
                "output_path": "",
                "summary": "",
                "output": "",
            },
        )
        for key, value in values.items():
            if value:
                current[key] = value

    for obj in scoped:
        role, content = _message_role_content(obj)
        if role == "assistant":
            visible.extend(_visible_text_blocks(content))
    for obj in event_records:
        _role, content = _message_role_content(obj)
        blocks = content if isinstance(content, list) else []
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    if name:
                        tool_calls.append(str(name))
                    tool_input = block.get("input")
                    if (
                        isinstance(tool_input, dict)
                        and tool_input.get("run_in_background") is True
                        and block.get("id")
                    ):
                        background_tool_ids.add(str(block["id"]))
                elif block.get("type") == "tool_result":
                    blob = block.get("content")
                    if not isinstance(blob, str):
                        continue
                    pending = re.search(
                        r"background with ID:\s*([A-Za-z0-9_-]+).*?"
                        r"Output is being written to:\s*(/[^\s]+)",
                        blob,
                        re.DOTALL | re.IGNORECASE,
                    )
                    result_tool_id = str(block.get("tool_use_id") or "")
                    if pending and result_tool_id in background_tool_ids:
                        remember_task(
                            pending.group(1),
                            tool_use_id=result_tool_id,
                            status="pending",
                            output_path=pending.group(2).rstrip(".,;"),
                        )
        blobs: List[str] = []
        if isinstance(content, str):
            blobs.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    value = block.get("content")
                    if isinstance(value, str):
                        blobs.append(value)
        queue_blob = obj.get("content")
        trusted_notification = (
            obj.get("type") == "queue-operation"
            and obj.get("operation") == "enqueue"
        ) or (
            _role == "user"
            and (
                (
                    isinstance(obj.get("origin"), dict)
                    and obj["origin"].get("kind") == "task-notification"
                )
                or obj.get("promptSource") == "system"
            )
        )
        if trusted_notification and isinstance(queue_blob, str):
            blobs.append(queue_blob)
        if not trusted_notification:
            blobs = []
        for blob in blobs:
            for match in _TASK_NOTIFICATION_RE.finditer(blob):
                body = match.group("body")
                task_id = _xml_value(body, "task-id")
                tool_id = _xml_value(body, "tool-use-id")
                known = task_by_id.get(task_id)
                if (
                    not known
                    or tool_id not in background_tool_ids
                    or known.get("tool_use_id") != tool_id
                ):
                    continue
                notice_path = _xml_value(body, "output-file")
                bound_path = str(known.get("output_path") or "")
                if (
                    not notice_path
                    or not bound_path
                    or os.path.realpath(notice_path) != os.path.realpath(bound_path)
                ):
                    # Task id + tool id are not enough: keep the output path
                    # immutably bound to the original tool_result.
                    continue
                remember_task(
                    task_id,
                    tool_use_id=tool_id,
                    status=_xml_value(body, "status") or "pending",
                    summary=_xml_value(body, "summary"),
                )

    substantive = ""
    for text in visible:
        if _is_substantive_visible_text(text):
            substantive = text.strip()
    if not substantive:
        # Backwards-compatible compact decisions ("halfway through LED-X")
        # remain useful; still reject quota boilerplate and narrated
        # transitions such as "Let me check CI".
        for text in visible:
            compact = text.strip()
            if (
                compact
                and not _QUOTA_TEXT_RE.search(compact)
                and not _TRANSITION_RE.search(compact)
            ):
                substantive = compact
    if substantive:
        # Cap is deliberately >=8K: the full LED-4056 / three-option decision
        # from the real quota transcript fits without truncating option 3 or
        # the recommendation.
        substantive = _redact_continuity_text(
            substantive[-_VISIBLE_TEXT_CAP:]
        )

    tasks: List[Dict[str, Any]] = []
    for task in task_by_id.values():
        checked = _validated_task_output(
            str(task.get("output_path") or ""), str(task.get("task_id") or "")
        )
        task["summary"] = _redact_continuity_text(
            str(task.get("summary") or "")[:2048]
        )
        output = _bounded_task_output(checked)
        if output:
            task["output"] = _redact_continuity_text(output)
        tasks.append(task)

    refs: List[str] = []
    ref_blob = "\n".join([substantive] + [str(t.get("output") or "") for t in tasks])
    for ref in _REF_RE.findall(ref_blob):
        normalized = re.sub(r"\s+", " ", ref).strip()
        if normalized.startswith("#"):
            normalized = "PR " + normalized
        if normalized not in refs:
            refs.append(normalized)

    return {
        "final_assistant_text": substantive,
        "tool_calls": tool_calls,
        "turns": len(scoped),
        # Bounded structured records are kept in-memory for deterministic
        # project resolution; callers must not persist them wholesale.
        "records": event_records,
        "logical_session_id": logical_id,
        "chat_session_id": chat_id,
        "transcript_id": Path(transcript_path).stem,
        "references": refs,
        "background_tasks": tasks,
    }
