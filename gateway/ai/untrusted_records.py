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

def frontend_tainted(item, *, store=None):
    """One origin predicate for selectors and inert rendering.

    A concrete FE-writable ledger path (or explicit ``store="ledger"``)
    opts into D8(4) for FE-writable ledger creations:
    missing origin is derived if source is absent, or a legacy FE write
    marker is present. Identifiable legacy host/system sources survive.
    Auxiliary FE/receiver stores have their own bounded fail-closed rule;
    host receipts and unrelated ledgers never inherit that capability.
    Verified out-of-band promotions retain the existing G-2 exception.
    """
    if not isinstance(item, dict):
        return False
    # Chained A2 records carry the guard stamp in a nested tags object.
    # Inspect it as additional provenance; an outer host tag never erases it.
    tags = item.get("tags")
    # A promotion receipt belongs to the enclosing item, never to its stamp.
    nested_tainted = isinstance(tags, dict) and frontend_tainted(
        {key: value for key, value in tags.items() if key != "promoted"}, store=store)
    source = str(item.get("source") or "").strip().casefold()
    origin = str(item.get("origin") or "").strip().casefold()
    tainted = (source in {"fin", "fin-lead", "front-end", "frontend"}
               or source.startswith(("frontend", "front-end:", "worker:", "assistant-proposal:"))
               or origin in {"front-end", "frontend"}
               or origin.startswith(("frontend:", "front-end:")))
    # Legacy Fin updates and accepted quarantine proposals may retain the
    # original host source. Their server-generated markers are provenance.
    legacy_fe = any(marker in str(item.get(field) or "").casefold()
                    for field in ("description", "last_note", "note")
                    for marker in ("[via fin", " - front end; server time",
                                   "proposed by a remote assistant"))
    tainted = tainted or legacy_fe or nested_tainted
    # The deployed assistant can write only these operations files (the
    # service's BindPaths). Strategy and other venture ledgers are read-only.
    # "ledger" is an explicit FE-writable-store context for in-memory callers;
    # real readers pass the concrete source file, never a record's claim.
    from pathlib import PurePath
    parts = PurePath(store).parts if isinstance(store, str) else ()
    fe_writable = store == "ledger" or (
        len(parts) >= 3 and parts[-3] == "ledger-v2"
        and parts[-2] in {"delimit", "domainvested", "wire-report"}
        and parts[-1] == "operations.jsonl")
    assistant_store = (len(parts) >= 2 and parts[-2] == "assistant"
                       and parts[-1] in {"captures.jsonl", "checkpoints.jsonl", "lead_writes.jsonl", "inbox.jsonl", "shared_pause.json"})
    assistant_transcript = (len(parts) >= 3 and parts[-3:-1] == ("assistant", "deliberations")
                            and parts[-1].endswith(".json"))
    # The frontend's own chained records and bookkeeping databases are
    # tunnel-writable, unlike owner/receipts.jsonl. They cannot confer host
    # authority when copied into an execution input without their tags.
    frontend_store = any(parts[i:i + 2] == (".delimit", "frontend")
                         for i in range(len(parts) - 1))
    private_store = False
    if parts and (parts[-1] == "index.jsonl" or re.fullmatch(r"pn-[0-9a-f]{12}\.json", parts[-1])):
        # Private roots are host-approved, retained read-only after config
        # removal, and never executable inputs. Include their records if a
        # consumer ever passes that concrete store to this origin predicate.
        try:
            from ai.frontend import a2_store
            private_store = any(PurePath(root).parts == parts[:len(PurePath(root).parts)]
                                for root in a2_store.approved_private_roots())
        except (OSError, ValueError, ImportError):
            pass
    # Receiver bookkeeping is host-owned, but these three stores contain
    # front-end initiated transfer/order records with no origin column. A
    # host signature or peer transport is not autonomous-origin evidence.
    executor_store = (len(parts) >= 2 and parts[-2] == "delimit-release"
                      and parts[-1] in {"audit.jsonl", "budget.sqlite", "executor.sqlite"})
    if not origin and (assistant_transcript or frontend_store or private_store or executor_store):
        tainted = True
    if not origin and ((fe_writable or assistant_store) and not source
                       or assistant_store and (source == "assistant" or item.get("untrusted") is True)):
        tainted = True
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

