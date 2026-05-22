"""Inbox-reply keyword classifier — extracted from inbox_daemon.py for
LED-2059 live-reload.

The inbox daemon is a long-running thread inside the gateway process.
Edits to keyword lists or detector regex landed on disk but didn't take
effect until the gateway restarted, which is why the founder's "ship the
symphony thread" reply (2026-04-28 incident) didn't auto-execute even
though the LED-820 fix was already on disk.

This module is reloaded by ``inbox_daemon.poll_once()`` at the start of
each poll via ``importlib.reload``. Code changes here pick up within one
poll interval (default 300s) without a gateway restart.

Three classifier signals layered by escalating intent:
- ``detect_approval_keywords`` — soft "approved" / "lgtm". Sets the draft
  to ``approved`` status; the founder still posts manually.
- ``detect_explicit_post_keywords`` — strong "ship it" / "post 812" /
  "autopost". The daemon is allowed to call ``auto_post_draft`` with a
  per-call DELIMIT_ENABLE_X_AUTOPOST bypass.
- ``detect_cancel_keywords`` — "cancel" / "hold" / "drop it". Marks the
  draft cancelled and skips any future processing for that id.

All three detectors strip quoted Gmail / Outlook history before scanning
so a quoted prior email containing one of the keywords doesn't trigger
the wrong branch (the LED-817 incident).
"""

from __future__ import annotations

import re
from typing import Iterable

# ── Keyword lists ────────────────────────────────────────────────────

# LED-817 (P0): word-boundary regex matching to prevent substring false
# positives from quoted Gmail history (e.g. "Reply 'hold' → I hold" in
# a quoted prior email tripping the cancel branch on an "approved" reply).
#
# LED-820 (P1) tier split: APPROVAL_KEYWORDS is the SOFT signal (mark
# approved, email founder for manual post — same as before). EXPLICIT_POST
# is the STRONG signal — caller authorized auto-execution of the draft's
# action right now, no second click required.

APPROVAL_KEYWORDS: list[str] = [
    "approved",
    "approve",
    "yes",
    "go ahead",
    "lgtm",
    "looks good",
]

# Explicit-post keywords — strong signal. Founder authorized auto-execution
# of the draft's action (post the tweet, comment the issue) WITHOUT a
# second click. Only triggers when both (a) one of these phrases is in the
# unquoted reply body AND (b) the draft has a registered draft_id match.
EXPLICIT_POST_KEYWORDS: list[str] = [
    "post it",
    "ship it",
    "post 8",        # "post 812", "post 800", etc. — LED-id-prefixed posts
    "post led",      # "post LED-812"
    "publish it",
    "send it",
    "go post",
    "post via api",
    "autopost",
]

CANCEL_KEYWORDS: list[str] = [
    "cancel",
    "stop",
    "abort",
    "don't post",
    "do not post",
    "hold",
    "skip",
    "drop it",
]


# ── Regex compilation ────────────────────────────────────────────────

def _compile_keyword_regex(keywords: Iterable[str]) -> re.Pattern[str]:
    """LED-817: build a strict word-boundary regex. Stricter than ``\\b``
    because hyphens count as word boundaries in Python — ``\\bstop\\b``
    matches the 'stop' in 'non-stop', re-introducing the substring bug
    we're trying to fix. Use ``(?<![\\w-])`` / ``(?![\\w-])`` to treat
    hyphens as internal so 'non-stop' doesn't trigger 'stop' but
    'please cancel.' still triggers 'cancel'.
    """
    parts: list[str] = []
    for kw in keywords:
        if " " in kw or "'" in kw:
            # Multi-word phrase — exact escape, internal whitespace
            # already provides separation.
            parts.append(re.escape(kw))
        else:
            parts.append(rf"(?<![\w-]){re.escape(kw)}(?![\w-])")
    return re.compile("(" + "|".join(parts) + ")", re.IGNORECASE)


_APPROVAL_RE = _compile_keyword_regex(APPROVAL_KEYWORDS)
_CANCEL_RE = _compile_keyword_regex(CANCEL_KEYWORDS)
_EXPLICIT_POST_RE = _compile_keyword_regex(EXPLICIT_POST_KEYWORDS)


# ── Quoted-email stripping ──────────────────────────────────────────

# LED-817 (P0): strip quoted email content before keyword scanning.
# Gmail (and most clients) preserve quoted history below the reply.
# Without stripping, a substring like "hold" from a previously-quoted
# email of mine triggered cancel on an "approved" reply. Detect quote
# markers and cut everything from the first marker onward.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^On\s+.+?\s+wrote:\s*$", re.MULTILINE),       # Gmail
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:\s*.+?\s*<", re.MULTILINE),             # Outlook
    re.compile(r"^Sent from my", re.MULTILINE),                # Mobile sig
)

# Lines starting with ">" are quoted in plaintext email
_QUOTED_LINE_PREFIX_RE = re.compile(r"^[\s]*>", re.MULTILINE)


def _strip_quoted_content(text: str) -> str:
    """Remove quoted email history so keyword scans only see the new reply.

    Cuts at the first quote marker found anywhere in the body, then drops
    any remaining lines that start with '>'. The intent is conservative:
    if a marker is ambiguous, we keep the text. False negatives (failing
    to strip) cause the same false-positive bug we're fixing, so the
    detection has to favor cutting too aggressively rather than too little.
    """
    if not text:
        return ""

    # Find the earliest position of any quote marker
    earliest = len(text)
    for pattern in _QUOTE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < earliest:
            earliest = match.start()

    head = text[:earliest]

    # Drop ">"-prefixed lines from the head (in case Gmail used ">" without
    # a "On X wrote:" header, or the user manually quoted).
    cleaned_lines = [
        line for line in head.splitlines()
        if not _QUOTED_LINE_PREFIX_RE.match(line)
    ]
    return "\n".join(cleaned_lines).strip()


# ── Public detectors ────────────────────────────────────────────────

def detect_approval_keywords(text: str) -> bool:
    """Soft-signal approval. Returns True if ``text`` (after stripping
    quoted history) contains an approval keyword on a word boundary.

    Guards against feedback loops:
    - Ignores emails FROM the daemon itself (contain "post this manually")
    - Ignores pathological one-word spam ("test" / "hello" / "approve me")
    - Otherwise relies on the upstream draft_id match to filter
    """
    if not text:
        return False
    body = _strip_quoted_content(text).lower().strip()
    if not body:
        return False

    # Block feedback loop: daemon's own confirmation emails
    if "post this manually" in body or "has been approved" in body:
        return False

    # LED-817 (P0): the previous junk-block dropped bare "approved" /
    # "approve" replies under the assumption they were spam. With the
    # upstream `draft_id and detect_approval_keywords` gate at the
    # callsite, a bare "approved" can only fire when the reply is in a
    # signed-draft thread — i.e. founder-intent. Keep the spam guard
    # only for the truly pathological cases.
    if body in ("test", "hello", "approve me"):
        return False

    return bool(_APPROVAL_RE.search(body))


def detect_cancel_keywords(text: str) -> bool:
    """LED-817: word-boundary regex against the unquoted reply only,
    no longer trips on 'hold' inside quoted history."""
    if not text:
        return False
    body = _strip_quoted_content(text).lower().strip()
    if not body:
        return False
    return bool(_CANCEL_RE.search(body))


def detect_explicit_post_keywords(text: str) -> bool:
    """LED-820 (P1): strong-signal trigger that authorizes the daemon to
    actually execute the draft's action (post the tweet, comment the
    issue) instead of merely marking it approved.

    Returns True only when the unquoted reply body contains an unambiguous
    posting directive ("post it" / "ship it" / "post 812" / etc).
    Generic approvals like "approved" / "lgtm" do NOT auto-execute — the
    founder must explicitly direct the post.
    """
    if not text:
        return False
    body = _strip_quoted_content(text).lower().strip()
    if not body:
        return False
    return bool(_EXPLICIT_POST_RE.search(body))
