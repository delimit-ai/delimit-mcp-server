"""Public context provenance and inert rendering, shared with Fin.

No frontend service is needed to read customer ledger or memory records.
Promotion stays fail closed when the private receipt store is unavailable.
This module exposes no tool, credential reader, executor or authority setter.
"""
from __future__ import annotations

import re
import unicodedata

MAX_UNTRUSTED_FRAGMENT_CHARS = 240

# Invisible / direction-altering characters. Trojan-source style bidi
# overrides and zero-width joiners let text render differently from what a
# reviewer (or a regex) sees, so they are removed on input and refused on
# output.
_INVISIBLE_CHARS = (
    "​‌‍‎‏"  # zero-width space/non-joiner/joiner, LRM, RLM
    "‪‫‬‭‮"  # bidi embeddings / overrides
    "⁠⁡⁢⁣⁤"  # word joiner, invisible operators
    "⁦⁧⁨⁩"        # bidi isolates
    "﻿"                          # BOM / zero-width no-break space
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")


def clean_untrusted(text: object, *, max_len: int = MAX_UNTRUSTED_FRAGMENT_CHARS) -> str:
    """Collapse an untrusted value to one inert line: control characters and
    line breaks become spaces, invisible/bidi characters are dropped, runs of
    whitespace collapse, and the result is truncated to ``max_len`` chars."""
    s = "" if text is None else str(text)
    s = _INVISIBLE_RE.sub("", s)
    s = "".join(" " if (unicodedata.category(c) == "Cc") else c for c in s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_len and len(s) > max_len:
        s = s[: max(0, max_len - 1)].rstrip() + "…"
    return s

def md_code_span(text: object, *, max_len: int = MAX_UNTRUSTED_FRAGMENT_CHARS) -> str:
    """Render an untrusted value as a single Markdown inline code span.

    The fence is one backtick longer than the longest backtick run inside the
    value, so the value cannot close the span early (CommonMark §6.1). A value
    that begins or ends with a backtick, or is empty, is space-padded so the
    fence parses unambiguously. Inside the span GitHub renders mentions,
    ``#123`` references, links, images and HTML literally."""
    s = clean_untrusted(text, max_len=max_len)
    if not s:
        s = "(empty)"
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    if s.startswith("`") or s.endswith("`"):
        s = f" {s} "
    return f"{fence}{s}{fence}"

def wrap_untrusted(label: str, text: str) -> str:
    """Frame source data for a model; callers must screen and handle hits first.

    The label is kept on one line; a delimiter collision is refused rather than
    allowing untrusted text to escape its frame.
    """
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
        raise ValueError("invalid untrusted-data label")
    if not isinstance(text, str) or re.search(r"<\s*/?\s*untrusted-data\b", text, re.I):
        raise ValueError("untrusted-data delimiter collision")
    return f'<untrusted-data label="{label}">\n{text}\n</untrusted-data>'

def _promotion_verified(item):
    """G-2: a host-CLI promotion record whose receipt verifies against the
    A2 requests store. Anything missing, malformed or unverifiable is not a
    promotion (fail closed)."""
    promoted = item.get("promoted")
    if not isinstance(promoted, dict) or promoted.get("by") != "owner-host":
        return False
    receipt, at = promoted.get("receipt_sha256"), promoted.get("at")
    if not isinstance(receipt, str) or len(receipt) != 64 or not isinstance(at, str) or not at:
        return False
    try:
        from ai.frontend import a2_store
        return a2_store.promotion_granted(str(item.get("id") or ""), receipt)
    except Exception:  # noqa: BLE001 - an unreadable store never promotes
        return False

def frontend_tainted(item):
    """Records supplied by a front end or a worker are data, never autonomous work.

    PR-0 keyed this on ``source`` and the front-end markers. A2 (G-2) extends
    it to the §4 origin tags: any explicit ``origin`` other than
    ``owner-host`` (``frontend:*``, ``worker:*``, ``worker:derived`` or a
    value the lattice does not recognise) is tainted, unless the item carries
    a host-CLI promotion whose receipt verifies. An item with no ``origin``
    and no front-end ``source`` stays owner-written (legacy items, Q5).
    """
    if not isinstance(item, dict):
        return False
    source = str(item.get("source") or "").strip().casefold()
    origin = str(item.get("origin") or "").strip().casefold()
    tainted = (source in {"fin-lead", "front-end", "frontend"}
               or source.startswith(("frontend:", "front-end:", "worker:"))
               or origin in {"front-end", "frontend"}
               or origin.startswith(("frontend:", "front-end:")))
    if not tainted and item.get("origin") is not None:
        # T-1: a missing origin is handled above (legacy owner item); a
        # present origin is trusted only as the exact string "owner-host".
        tainted = item.get("origin") != "owner-host"
    return tainted and not _promotion_verified(item)

MAX_QUOTED = 400

def non_owner(record: object) -> bool:
    return isinstance(record, dict) and frontend_tainted(record)

def label(record: dict) -> str:
    """The frame label: the record's origin (or source), as a safe token."""
    raw = str(record.get("origin") or record.get("source") or "untrusted")
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64]
    return token or "untrusted"

def quote_text(text: object, frame_label: str, *, max_len: int = MAX_QUOTED) -> str:
    """One inert line inside an untrusted-data frame. Angle brackets are
    neutralised first, so the value can never close or forge the frame."""
    line = clean_untrusted(text, max_len=max_len).replace("<", "‹").replace(">", "›")
    return wrap_untrusted(frame_label, line)

def quote_inline(text: object, frame_label: str, *, max_len: int = 200) -> str:
    """Single-line form for Markdown indexes (MEMORY projection)."""
    return f"[quoted data from {frame_label}] {md_code_span(text, max_len=max_len)}"

def quote_record(record: dict, fields: tuple[str, ...]) -> dict:
    """A copy whose text ``fields`` are quoted when the record is non-owner.
    Owner records are returned unchanged (same object)."""
    if not non_owner(record):
        return record
    frame = label(record)
    quoted = dict(record)
    for name in fields:
        if name in quoted and quoted[name] is not None:
            quoted[name] = quote_text(quoted[name], frame)
    quoted["quoted"] = True
    return quoted

