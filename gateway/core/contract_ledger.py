"""
Delimit Contract Ledger
Reads, validates, and queries the append-only JSONL event ledger.
Optional SQLite index for fast lookups (never required for CI).

Per Jamsons Doctrine:
- Deterministic outputs
- Append-only artifacts
- SQLite index is optional, not required for CI
- No telemetry collection
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .event_schema import compute_event_hash, validate_event

logger = logging.getLogger("delimit.contract_ledger")

GENESIS_HASH = "GENESIS"


class ChainValidationError(Exception):
    """Raised when the ledger hash chain is broken."""

    def __init__(self, index: int, expected: str, actual: str):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Hash chain broken at event {index}: "
            f"expected previous_hash={expected!r}, got={actual!r}"
        )


class ContractLedger:
    """Read, validate, and query the JSONL event ledger."""

    def __init__(self, ledger_path: str):
        """Initialize with path to the JSONL ledger file.

        Args:
            ledger_path: Path to events.jsonl file.
        """
        self._ledger_path = Path(ledger_path)

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    def exists(self) -> bool:
        """Check if the ledger file exists."""
        return self._ledger_path.exists()

    def read_events(self) -> List[Dict[str, Any]]:
        """Read all events from the JSONL ledger.

        Returns:
            List of event dictionaries in chronological order.
            Empty list if ledger does not exist or is empty.
        """
        if not self._ledger_path.exists():
            return []

        events = []
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                        events.append(event)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Skipping malformed JSON at line %d: %s", line_num, e
                        )
        except OSError as e:
            logger.warning("Failed to read ledger %s: %s", self._ledger_path, e)

        return events

    def get_latest_event(self) -> Optional[Dict[str, Any]]:
        """Return the most recent event, or None if ledger is empty."""
        if not self._ledger_path.exists():
            return None

        last_line = ""
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
        except OSError as e:
            logger.warning("Failed to read ledger: %s", e)
            return None

        if not last_line:
            return None

        try:
            return json.loads(last_line)
        except json.JSONDecodeError:
            return None

    def get_event_count(self) -> int:
        """Return the number of events in the ledger."""
        if not self._ledger_path.exists():
            return 0

        count = 0
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except OSError:
            pass
        return count

    def validate_chain(self) -> bool:
        """Validate the entire hash chain integrity.

        Checks that:
        1. First event has previous_hash == GENESIS
        2. Each subsequent event's previous_hash matches the prior event_hash
        3. Each event's event_hash is correctly computed

        Returns:
            True if the chain is valid.

        Raises:
            ChainValidationError: If the chain is broken.
        """
        events = self.read_events()
        if not events:
            return True

        for i, event in enumerate(events):
            # Validate previous_hash linkage
            if i == 0:
                if event.get("previous_hash") != GENESIS_HASH:
                    raise ChainValidationError(
                        index=i,
                        expected=GENESIS_HASH,
                        actual=event.get("previous_hash", ""),
                    )
            else:
                expected_prev = events[i - 1].get("event_hash", "")
                actual_prev = event.get("previous_hash", "")
                if actual_prev != expected_prev:
                    raise ChainValidationError(
                        index=i,
                        expected=expected_prev,
                        actual=actual_prev,
                    )

            # Validate event_hash correctness
            expected_hash = compute_event_hash(
                previous_hash=event.get("previous_hash", ""),
                spec_hash=event.get("spec_hash", ""),
                diff_summary=event.get("diff_summary", []),
                commit=event.get("commit", ""),
                timestamp=event.get("timestamp", ""),
            )
            actual_hash = event.get("event_hash", "")
            if actual_hash != expected_hash:
                raise ChainValidationError(
                    index=i,
                    expected=f"computed={expected_hash}",
                    actual=f"stored={actual_hash}",
                )

        return True

    def get_api_timeline(self, api_name: str) -> List[Dict[str, Any]]:
        """Return all events for a specific API in chronological order.

        Args:
            api_name: The API name to filter by.

        Returns:
            List of events matching the api_name.
        """
        return [
            event for event in self.read_events()
            if event.get("api_name") == api_name
        ]

    def get_events_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        """Return all events of a specific type."""
        return [
            event for event in self.read_events()
            if event.get("event_type") == event_type
        ]

    def get_events_by_repository(self, repository: str) -> List[Dict[str, Any]]:
        """Return all events for a specific repository."""
        return [
            event for event in self.read_events()
            if event.get("repository") == repository
        ]


class SQLiteIndex:
    """Optional SQLite index for fast ledger queries.

    This is a convenience layer that is NEVER required for CI execution.
    The JSONL ledger is the source of truth.
    """

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS events (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        event_hash TEXT UNIQUE NOT NULL,
        event_type TEXT NOT NULL,
        api_name TEXT NOT NULL,
        repository TEXT NOT NULL,
        version TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        actor TEXT NOT NULL,
        spec_hash TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        policy_result TEXT NOT NULL,
        complexity_score INTEGER NOT NULL,
        complexity_class TEXT NOT NULL,
        raw_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_api_name ON events(api_name);
    CREATE INDEX IF NOT EXISTS idx_repository ON events(repository);
    CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp);
    """

    def __init__(self, db_path: str):
        """Initialize SQLite index.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(self.SCHEMA_SQL)
        return self._conn

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def index_event(self, event: Dict[str, Any]) -> bool:
        """Add a single event to the SQLite index.

        Returns True on success, False on failure.
        """
        try:
            conn = self._connect()
            conn.execute(
                """INSERT OR IGNORE INTO events
                (event_hash, event_type, api_name, repository, version,
                 timestamp, commit_sha, actor, spec_hash, previous_hash,
                 policy_result, complexity_score, complexity_class, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_hash"],
                    event["event_type"],
                    event["api_name"],
                    event["repository"],
                    event["version"],
                    event["timestamp"],
                    event["commit"],
                    event["actor"],
                    event["spec_hash"],
                    event["previous_hash"],
                    event["policy_result"],
                    event["complexity_score"],
                    event["complexity_class"],
                    json.dumps(event, sort_keys=True),
                ),
            )
            conn.commit()
            return True
        except (sqlite3.Error, KeyError) as e:
            logger.warning("Failed to index event: %s", e)
            return False

    def rebuild_from_ledger(self, ledger: ContractLedger) -> int:
        """Rebuild the entire SQLite index from the JSONL ledger.

        Returns the number of events indexed.
        """
        events = ledger.read_events()
        count = 0
        for event in events:
            if self.index_event(event):
                count += 1
        return count

    def query_by_api(self, api_name: str) -> List[Dict[str, Any]]:
        """Query events by API name using the index."""
        try:
            conn = self._connect()
            cursor = conn.execute(
                "SELECT raw_json FROM events WHERE api_name = ? ORDER BY timestamp",
                (api_name,),
            )
            return [json.loads(row["raw_json"]) for row in cursor]
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logger.warning("SQLite query failed: %s", e)
            return []

    def query_by_repository(self, repository: str) -> List[Dict[str, Any]]:
        """Query events by repository using the index."""
        try:
            conn = self._connect()
            cursor = conn.execute(
                "SELECT raw_json FROM events WHERE repository = ? ORDER BY timestamp",
                (repository,),
            )
            return [json.loads(row["raw_json"]) for row in cursor]
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logger.warning("SQLite query failed: %s", e)
            return []

    def get_event_count(self) -> int:
        """Return total number of indexed events."""
        try:
            conn = self._connect()
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM events")
            return cursor.fetchone()["cnt"]
        except sqlite3.Error:
            return 0
