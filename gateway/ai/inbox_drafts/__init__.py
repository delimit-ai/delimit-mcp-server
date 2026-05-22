"""Inbox drafts registry — LED-1129 Phase 1.

Foundation for the autonomous-executor that closes the email→action loop.
Phase 1 (this module): schema, canonicalization, HMAC binding, SQLite registry.
NO behavior change — drafts get registered + signed; nobody consumes them yet.
Phase 2 will add the separate-process executor that reads this registry.

See docs/inbox_executor_v1.md for the canonicalization + state-machine spec.
"""

from ai.inbox_drafts.schema import (
    DEFAULT_TTL_SECONDS,
    HMAC_KEY_PATH,
    DraftKind,
    DraftStatus,
    SignedDraft,
    canonicalize,
    content_hash,
    new_draft_id,
    sign_draft,
    verify_draft,
)
from ai.inbox_drafts.registry import (
    DEFAULT_DB_PATH,
    DraftRow,
    expire_pending,
    find_draft_by_led_ref,
    get_draft,
    insert_draft,
    list_attempts,
    list_drafts,
    migrate,
    record_attempt,
    transition,
)

__all__ = [
    # schema
    "DEFAULT_TTL_SECONDS",
    "HMAC_KEY_PATH",
    "DraftKind",
    "DraftStatus",
    "SignedDraft",
    "canonicalize",
    "content_hash",
    "new_draft_id",
    "sign_draft",
    "verify_draft",
    # registry
    "DEFAULT_DB_PATH",
    "DraftRow",
    "expire_pending",
    "find_draft_by_led_ref",
    "get_draft",
    "insert_draft",
    "list_attempts",
    "list_drafts",
    "migrate",
    "record_attempt",
    "transition",
]
