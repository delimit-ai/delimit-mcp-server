"""SQLite draft registry — LED-1129 Phase 1 PR-2.

Two-table shape (per the deliberation):

  drafts    — durable state machine. One row per draft_id.
              Columns mirror SignedDraft + status + lifecycle timestamps.
  attempts  — execution history. One row per execute attempt against a
              draft_id. Forensics for failures + replay-detection.

State transitions are enforced atomically via SQLite transactions. The
schema layer (schema.py) owns the cryptography; this layer owns the
durable state. The executor (Phase 2) consumes from this layer.

Crash semantics: a row at status='executing' after a process restart
surfaces for human reconciliation — we do NOT auto-retry. That's the
at-most-once contract.

Concurrency: SQLite WAL mode + UPDATE ... WHERE status=? gives us atomic
state transitions without explicit file locking. Multiple readers OK;
the executor takes a row by transitioning approved→executing in a single
UPDATE that returns rowcount.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ai.inbox_drafts.schema import (
    DEFAULT_TTL_SECONDS,
    DraftStatus,
    SignedDraft,
)

DEFAULT_DB_PATH = Path.home() / ".delimit" / "drafts.db"

# Schema version for the SQLite tables themselves. Distinct from the
# draft schema_version (the JSON contract) — this one tracks DB migrations.
DB_SCHEMA_VERSION = 1


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    """Resolve db_path arg, reading module default at call time.

    Reading at call time (rather than as a default-arg) lets tests
    monkeypatch `ai.inbox_drafts.registry.DEFAULT_DB_PATH` and have the
    change propagate. With default-arg capture, the value is bound at
    function-definition time and monkeypatching is invisible.
    """
    if db_path is not None:
        return db_path
    import ai.inbox_drafts.registry as _self
    return _self.DEFAULT_DB_PATH


# ── Migrations ────────────────────────────────────────────────────────


_MIGRATIONS = [
    # v1: initial schema
    """
    CREATE TABLE IF NOT EXISTS drafts (
        draft_id           TEXT PRIMARY KEY,
        draft_kind         TEXT NOT NULL,
        target_json        TEXT NOT NULL,
        payload_json       TEXT NOT NULL,
        issued_at          INTEGER NOT NULL,
        key_version        INTEGER NOT NULL,
        schema_version     TEXT NOT NULL,
        content_hash       TEXT NOT NULL,
        signature          TEXT NOT NULL,
        status             TEXT NOT NULL,
        led_ref            TEXT,
        approval_subject   TEXT,
        executed_url       TEXT,
        last_error         TEXT,
        created_at         INTEGER NOT NULL,
        updated_at         INTEGER NOT NULL,
        completed_at       INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
    CREATE INDEX IF NOT EXISTS idx_drafts_issued_at ON drafts(issued_at);
    CREATE INDEX IF NOT EXISTS idx_drafts_led_ref ON drafts(led_ref);

    CREATE TABLE IF NOT EXISTS attempts (
        attempt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id     TEXT NOT NULL,
        kind         TEXT NOT NULL,           -- "verify" | "execute"
        outcome      TEXT NOT NULL,           -- "ok" | "failed" | "skipped"
        reason       TEXT,
        executed_url TEXT,
        attempted_at INTEGER NOT NULL,
        FOREIGN KEY (draft_id) REFERENCES drafts(draft_id)
    );

    CREATE INDEX IF NOT EXISTS idx_attempts_draft_id ON attempts(draft_id);

    CREATE TABLE IF NOT EXISTS db_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL gives us cleaner concurrent-reader semantics than the default
    # rollback journal. busy_timeout makes blocked writers wait briefly
    # instead of immediately raising — keeps the executor's poll loop
    # robust against transient daemon writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(db_path: Optional[Path] = None) -> int:
    """Apply pending migrations. Returns the resulting DB schema version.

    Idempotent — running again on an up-to-date DB is a no-op.
    """
    db_path = _resolve_db_path(db_path)
    conn = _open(db_path)
    try:
        # Bootstrap meta table so we can read the version.
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS db_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        cur = conn.execute("SELECT value FROM db_meta WHERE key = 'db_schema_version'")
        row = cur.fetchone()
        current = int(row["value"]) if row else 0
        for i, sql in enumerate(_MIGRATIONS, start=1):
            if i > current:
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)",
                    ("db_schema_version", str(i)),
                )
        return DB_SCHEMA_VERSION
    finally:
        conn.close()


# ── DAO ───────────────────────────────────────────────────────────────


@dataclass
class DraftRow:
    draft_id: str
    draft_kind: str
    target: Dict[str, Any]
    payload: Any
    issued_at: int
    key_version: int
    schema_version: str
    content_hash: str
    signature: str
    status: str
    led_ref: Optional[str]
    approval_subject: Optional[str]
    executed_url: Optional[str]
    last_error: Optional[str]
    created_at: int
    updated_at: int
    completed_at: Optional[int]

    @classmethod
    def from_sqlite_row(cls, row: sqlite3.Row) -> "DraftRow":
        return cls(
            draft_id=row["draft_id"],
            draft_kind=row["draft_kind"],
            target=json.loads(row["target_json"]),
            payload=json.loads(row["payload_json"]),
            issued_at=row["issued_at"],
            key_version=row["key_version"],
            schema_version=row["schema_version"],
            content_hash=row["content_hash"],
            signature=row["signature"],
            status=row["status"],
            led_ref=row["led_ref"],
            approval_subject=row["approval_subject"],
            executed_url=row["executed_url"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def to_signed_dict(self) -> Dict[str, Any]:
        """Return only the fields that are part of the HMAC scope.

        Used by the executor to re-verify the signature before acting.
        """
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


@contextmanager
def connection(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Ensures migrations are applied first."""
    db_path = _resolve_db_path(db_path)
    migrate(db_path)
    conn = _open(db_path)
    try:
        yield conn
    finally:
        conn.close()


def insert_draft(
    signed: SignedDraft,
    *,
    led_ref: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Insert a freshly-signed draft in PENDING state.

    Raises sqlite3.IntegrityError if the draft_id already exists — by
    construction (ULID) this only happens on real ID collision (~impossible)
    or replay attempt with the same id, both of which we want to refuse.
    """
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO drafts (
                draft_id, draft_kind, target_json, payload_json, issued_at,
                key_version, schema_version, content_hash, signature, status,
                led_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signed.draft_id,
                signed.draft_kind,
                json.dumps(signed.target, sort_keys=True),
                json.dumps(signed.payload, sort_keys=True),
                signed.issued_at,
                signed.key_version,
                signed.schema_version,
                signed.content_hash,
                signed.signature,
                DraftStatus.PENDING.value,
                led_ref,
                now,
                now,
            ),
        )


def get_draft(draft_id: str, db_path: Optional[Path] = None) -> Optional[DraftRow]:
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        return DraftRow.from_sqlite_row(row) if row else None


def find_draft_by_led_ref(led_ref: str, db_path: Optional[Path] = None) -> List[DraftRow]:
    """Return drafts associated with a given LED reference.

    Used by the executor when matching founder Ship-it replies whose
    subject line carries an [LED-XXXX] tag.
    """
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE led_ref = ? ORDER BY created_at DESC",
            (led_ref,),
        ).fetchall()
        return [DraftRow.from_sqlite_row(r) for r in rows]


def transition(
    draft_id: str,
    *,
    expected: str,
    new: str,
    db_path: Optional[Path] = None,
    approval_subject: Optional[str] = None,
    executed_url: Optional[str] = None,
    last_error: Optional[str] = None,
    completed: bool = False,
) -> bool:
    """Atomically move a draft from `expected` → `new`.

    Returns True iff the transition occurred (the row was in `expected`
    state at the moment of the UPDATE). Returns False otherwise — the
    caller did not win the race or the row is in a different state.

    This is the at-most-once primitive: the executor calls
    transition(approved → executing) before any side effect; the
    rowcount tells it whether it owns the action.
    """
    now = int(time.time())
    with connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE drafts SET
                status            = ?,
                approval_subject  = COALESCE(?, approval_subject),
                executed_url      = COALESCE(?, executed_url),
                last_error        = COALESCE(?, last_error),
                completed_at      = CASE WHEN ? = 1 THEN ? ELSE completed_at END,
                updated_at        = ?
            WHERE draft_id = ? AND status = ?
            """,
            (
                new,
                approval_subject,
                executed_url,
                last_error,
                1 if completed else 0,
                now,
                now,
                draft_id,
                expected,
            ),
        )
        return cur.rowcount == 1


def expire_pending(
    db_path: Optional[Path] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> int:
    """Mark pending drafts older than TTL as EXPIRED.

    Returns the count expired. Idempotent.
    """
    now = int(time.time())
    cutoff = now - ttl_seconds
    with connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE drafts SET status = ?, updated_at = ?
             WHERE status = ? AND issued_at < ?
            """,
            (DraftStatus.EXPIRED.value, now, DraftStatus.PENDING.value, cutoff),
        )
        return cur.rowcount


def record_attempt(
    draft_id: str,
    *,
    kind: str,
    outcome: str,
    reason: Optional[str] = None,
    executed_url: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Append an attempt row. Returns the new attempt_id.

    `kind`: "verify" | "execute"
    `outcome`: "ok" | "failed" | "skipped"
    """
    now = int(time.time())
    with connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO attempts (draft_id, kind, outcome, reason, executed_url, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (draft_id, kind, outcome, reason, executed_url, now),
        )
        return cur.lastrowid


def list_attempts(draft_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE draft_id = ? ORDER BY attempt_id ASC",
            (draft_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_drafts(
    status: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> List[DraftRow]:
    with connection(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [DraftRow.from_sqlite_row(r) for r in rows]
