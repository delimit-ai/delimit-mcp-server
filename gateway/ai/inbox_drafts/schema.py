"""Draft schema, canonicalization, and HMAC primitives — LED-1129 Phase 1.

This is the foundation layer. Other layers (registry, executor) build on top.

Key invariants:

- Every draft gets a ULID `draft_id` (sortable, unique, NOT derived from content).
- Every draft is signed with HMAC-SHA256 over a canonical byte representation
  of (draft_id, draft_kind, target, payload, issued_at, key_version) — the HMAC
  scope binds a signature to one concrete instance, so subject-line collisions
  or thread reuse cannot replay an approval against a different draft.
- Canonical JSON form is RFC 8785 (sorted keys, no whitespace, UTF-8 NFC).
  Written down here AND in docs/inbox_executor_v1.md so the daemon writer
  and the executor reader cannot silently disagree on bytes.
- HMAC key lives at ~/.delimit/secrets/inbox-executor-hmac.key (mode 600).
  Distinct from wrap-hmac.key — separation of concerns. Auto-generated on
  first sign call if missing.
- Schema is versioned via `schema_version` field. v1 is the only version;
  future migrations bump it.
- Keys are versioned via `key_version`. Rotation issues a new version side
  by side; verifier tries the registered version first.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import os
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


SCHEMA_VERSION = "v1"
HMAC_KEY_PATH = Path.home() / ".delimit" / "secrets" / "inbox-executor-hmac.key"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h per the LED-1129 deliberation


class DraftKind(str, enum.Enum):
    """Allowlisted action types the executor will dispatch on (Phase 2+).

    Anything outside this enum terminates at `founder_directive_acked` and
    requires a Claude session to execute. Adding a new kind is itself an
    authority_class_expansion event under STR-183 — needs founder attestation.
    """

    GITHUB_COMMENT = "github_comment"
    SOCIAL_POST = "social_post"
    LEDGER_DONE = "ledger_done"
    NOTIFY_ROUTING_UPDATE = "notify_routing_update"
    DEPLOY_PUBLISH_PREVALIDATED_ARTIFACT = "deploy_publish_prevalidated_artifact"


class DraftStatus(str, enum.Enum):
    """State machine for a single draft.

    Transitions (the only legal ones):
      pending  → approved   (HMAC verified + founder Ship-it match)
      pending  → expired    (TTL elapsed)
      pending  → cancelled  (founder reply detected as negative/cancel)
      approved → executing  (executor takes the row, before side effect)
      executing → completed (executor finished + recorded executed_url)
      executing → completed_with_error (executor finished but action failed)
      *        → terminal_unrecoverable  (only set by human reconciliation)

    A row stuck at `executing` after a process restart surfaces for human
    reconciliation — we do NOT auto-retry. That's at-most-once semantics.
    """

    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERROR = "completed_with_error"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    TERMINAL_UNRECOVERABLE = "terminal_unrecoverable"


# ── ULID ──────────────────────────────────────────────────────────────

# A minimal monotonic ULID generator. We do not pull a third-party dep;
# the gateway has no ulid package and this is the only call site.
# 26 chars: 10 char timestamp (ms since epoch, base32) + 16 chars randomness.
# Sortable lexicographically by time. NOT content-derived (that would defeat
# the purpose — we want monotonic IDs for index locality, content goes in
# the HMAC scope separately).
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base32


def new_draft_id() -> str:
    """Return a new ULID (26 chars, monotonic by time, urlsafe).

    Not cryptographically derived from content — content binding lives in
    the HMAC. The ID is just an instance identifier with index-locality
    properties.
    """
    ts_ms = int(time.time() * 1000)
    ts_part = ""
    n = ts_ms
    for _ in range(10):
        ts_part = _ULID_ALPHABET[n & 0x1F] + ts_part
        n >>= 5

    rand_bytes = secrets.token_bytes(10)
    rand_part = ""
    n = int.from_bytes(rand_bytes, "big")
    for _ in range(16):
        rand_part = _ULID_ALPHABET[n & 0x1F] + rand_part
        n >>= 5

    return ts_part + rand_part


# ── Canonical JSON (RFC 8785-style) ───────────────────────────────────


def canonicalize(value: Any) -> bytes:
    """Return the canonical byte representation of `value`.

    Per RFC 8785 / JSON Canonicalization Scheme, with simplifications:
    - Object keys sorted lexicographically (string sort, not byte sort).
    - No whitespace between tokens.
    - Strings normalized to Unicode NFC before serialization (so the same
      visual character always hashes the same regardless of input form).
    - Numbers serialized via Python's `json.dumps` default; no scientific
      notation reformatting (callers should pass ints/floats already in the
      form they want signed).
    - Booleans, None, lists pass through.

    The function is deterministic: feeding the same Python value at any time
    on any machine returns the same bytes. That's the only contract that
    matters for HMAC parity between daemon and executor.

    LIMITATION (documented, not fixed in v1): Python's float→str varies in
    the lowest bits between platforms. Phase 1 callers MUST stringify floats
    upstream if they need cross-host parity. We do not currently sign
    payloads with floats, but the spec doc warns about it.
    """
    return _canon(value).encode("utf-8")


def _canon(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        # See LIMITATION above. We use repr to keep round-trip stable on
        # the local host, but cross-host parity is not guaranteed for floats.
        return repr(v)
    if isinstance(v, str):
        return json.dumps(unicodedata.normalize("NFC", v), ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_canon(x) for x in v) + "]"
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: kv[0])
        return "{" + ",".join(
            json.dumps(unicodedata.normalize("NFC", k), ensure_ascii=False) + ":" + _canon(val)
            for k, val in items
        ) + "}"
    raise TypeError(f"canonicalize: unsupported type {type(v).__name__}")


# ── Content hash ──────────────────────────────────────────────────────


def content_hash(
    draft_id: str,
    draft_kind: str,
    target: Dict[str, Any],
    payload: Any,
    issued_at: int,
    key_version: int = 1,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    """SHA-256 hex digest binding all signed inputs together.

    Includes draft_id (instance binding), kind (semantic binding), target
    (where the action lands), payload (what the action does), issued_at
    (anti-replay anchor), and the key + schema versions (rotation safety).

    Returned hex string is what the HMAC key signs.
    """
    blob = canonicalize({
        "draft_id": draft_id,
        "draft_kind": draft_kind,
        "target": target,
        "payload": payload,
        "issued_at": issued_at,
        "key_version": key_version,
        "schema_version": schema_version,
    })
    return hashlib.sha256(blob).hexdigest()


# ── HMAC key management ───────────────────────────────────────────────


def _ensure_key(path: Optional[Path] = None) -> bytes:
    """Read the HMAC key, generating it on first call if missing.

    Mode 600 is enforced so other system users can't read it. Key is 32
    random bytes (256-bit), suitable for HMAC-SHA256.

    Resolves the default at call time (not as a default-arg) so tests can
    monkeypatch `ai.inbox_drafts.schema.HMAC_KEY_PATH` and have the change
    propagate.
    """
    if path is None:
        # Re-read module attribute each call; tests can monkeypatch.
        import ai.inbox_drafts.schema as _self
        path = _self.HMAC_KEY_PATH
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort on filesystems that don't support chmod (Windows).
        pass
    return key


# ── Draft sign / verify ───────────────────────────────────────────────


@dataclass
class SignedDraft:
    """The signed-draft record stored in the registry.

    All fields are part of the HMAC scope (see content_hash). Mutating any
    field after signing invalidates the signature; the verifier rejects.
    """

    draft_id: str
    draft_kind: str
    target: Dict[str, Any]
    payload: Any
    issued_at: int
    key_version: int
    schema_version: str
    content_hash: str
    signature: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "draft_kind": self.draft_kind,
            "target": self.target,
            "payload": self.payload,
            "issued_at": self.issued_at,
            "key_version": self.key_version,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "signature": self.signature,
        }


def sign_draft(
    draft_kind: str,
    target: Dict[str, Any],
    payload: Any,
    *,
    draft_id: Optional[str] = None,
    issued_at: Optional[int] = None,
    key_version: int = 1,
    key_path: Optional[Path] = None,
) -> SignedDraft:
    """Create a SignedDraft for a new action proposal.

    `draft_kind` should be one of DraftKind values; we don't enforce here
    so the schema layer stays stringly-typed (the registry/executor enforce
    the allowlist with proper errors at their boundaries).
    """
    if draft_id is None:
        draft_id = new_draft_id()
    if issued_at is None:
        issued_at = int(time.time())

    digest = content_hash(
        draft_id=draft_id,
        draft_kind=draft_kind,
        target=target,
        payload=payload,
        issued_at=issued_at,
        key_version=key_version,
        schema_version=SCHEMA_VERSION,
    )

    key = _ensure_key(key_path) if key_path else _ensure_key()
    sig = hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()

    return SignedDraft(
        draft_id=draft_id,
        draft_kind=draft_kind,
        target=target,
        payload=payload,
        issued_at=issued_at,
        key_version=key_version,
        schema_version=SCHEMA_VERSION,
        content_hash=digest,
        signature=sig,
    )


def verify_draft(
    record: Dict[str, Any],
    *,
    now: Optional[int] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    key_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Verify a stored draft record. Returns (ok, reason).

    Checks (fail-closed; first failure short-circuits):
    - schema_version is recognized
    - all required fields present
    - issued_at within TTL window (not stale, not in the future)
    - content_hash matches recomputed hash from fields
    - HMAC signature matches recomputed signature using key for key_version

    Phase 1 only supports key_version=1 (the only key extant). Phase 2
    adds the rotation lookup table.
    """
    required = {"draft_id", "draft_kind", "target", "payload", "issued_at",
                "key_version", "schema_version", "content_hash", "signature"}
    missing = required - set(record.keys())
    if missing:
        return False, f"missing fields: {sorted(missing)}"

    if record["schema_version"] != SCHEMA_VERSION:
        return False, f"unsupported schema_version: {record['schema_version']}"

    if record["key_version"] != 1:
        return False, f"unknown key_version: {record['key_version']}"

    if now is None:
        now = int(time.time())
    if record["issued_at"] > now + 60:  # 60s clock skew tolerance
        return False, "issued_at is in the future"
    if now - record["issued_at"] > ttl_seconds:
        return False, "draft expired (TTL elapsed)"

    expected_hash = content_hash(
        draft_id=record["draft_id"],
        draft_kind=record["draft_kind"],
        target=record["target"],
        payload=record["payload"],
        issued_at=record["issued_at"],
        key_version=record["key_version"],
        schema_version=record["schema_version"],
    )
    if not hmac.compare_digest(expected_hash, record["content_hash"]):
        return False, "content_hash mismatch (record was tampered)"

    key = _ensure_key(key_path) if key_path else _ensure_key()
    expected_sig = hmac.new(key, expected_hash.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, record["signature"]):
        return False, "signature mismatch (HMAC failed)"

    return True, "ok"
