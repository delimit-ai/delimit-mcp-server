"""
Delimit Event Backbone
Constructs ledger events, generates SHA-256 hashes, links hash chains,
and appends to the append-only JSONL ledger.

Per Jamsons Doctrine:
- Deterministic outputs
- Append-only artifacts
- Fail-closed CI behavior (ledger failures never affect CI)
- No telemetry collection
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .event_schema import (
    canonicalize,
    compute_event_hash,
    create_event,
    now_utc,
    validate_event,
)

logger = logging.getLogger("delimit.event_backbone")

# Default ledger location relative to repository root
DEFAULT_LEDGER_DIR = ".delimit/ledger"
DEFAULT_LEDGER_FILE = "events.jsonl"

# Genesis sentinel for first event in chain
GENESIS_HASH = "GENESIS"


class EventBackbone:
    """Constructs and appends ledger events with SHA-256 hash chain."""

    def __init__(self, ledger_dir: Optional[str] = None):
        """Initialize the backbone with a ledger directory.

        Args:
            ledger_dir: Path to ledger directory. Defaults to .delimit/ledger/
        """
        self._ledger_dir = Path(ledger_dir) if ledger_dir else Path(DEFAULT_LEDGER_DIR)
        self._ledger_file = self._ledger_dir / DEFAULT_LEDGER_FILE

    @property
    def ledger_path(self) -> Path:
        """Return the full path to the JSONL ledger file."""
        return self._ledger_file

    def _ensure_ledger_dir(self) -> bool:
        """Create ledger directory if it does not exist.

        Returns True if directory exists/was created, False on failure.
        """
        try:
            self._ledger_dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as e:
            logger.warning("Failed to create ledger directory %s: %s", self._ledger_dir, e)
            return False

    def get_last_event_hash(self) -> str:
        """Read the last event hash from the ledger for chain linking.

        Returns GENESIS if the ledger is empty or does not exist.
        """
        if not self._ledger_file.exists():
            return GENESIS_HASH

        try:
            last_line = ""
            with open(self._ledger_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped

            if not last_line:
                return GENESIS_HASH

            event = json.loads(last_line)
            return event.get("event_hash", GENESIS_HASH)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning("Failed to read last event hash: %s", e)
            return GENESIS_HASH

    def construct_event(
        self,
        event_type: str,
        api_name: str,
        repository: str,
        version: str,
        commit: str,
        actor: str,
        spec_hash: str,
        diff_summary: List[Any],
        policy_result: str,
        complexity_score: int,
        complexity_class: str,
        timestamp: Optional[str] = None,
        previous_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Construct a ledger event with computed hash chain.

        Args:
            event_type: Type of event (e.g. "contract_change")
            api_name: Name of the API
            repository: Repository identifier
            version: API version string
            commit: Git commit SHA
            actor: Who triggered the event
            spec_hash: SHA-256 hash of the API spec
            diff_summary: List of change summaries
            policy_result: Result of policy evaluation
            complexity_score: Complexity score 0-100
            complexity_class: Complexity classification
            timestamp: ISO 8601 UTC timestamp. Auto-generated if None.
            previous_hash: Previous event hash. Auto-read from ledger if None.

        Returns:
            Validated event dictionary with computed event_hash.

        Raises:
            ValueError: If the event fails schema validation.
        """
        if timestamp is None:
            timestamp = now_utc()

        if previous_hash is None:
            previous_hash = self.get_last_event_hash()

        return create_event(
            event_type=event_type,
            api_name=api_name,
            repository=repository,
            version=version,
            timestamp=timestamp,
            commit=commit,
            actor=actor,
            spec_hash=spec_hash,
            previous_hash=previous_hash,
            diff_summary=diff_summary,
            policy_result=policy_result,
            complexity_score=complexity_score,
            complexity_class=complexity_class,
        )

    def append_event(self, event: Dict[str, Any]) -> bool:
        """Append a validated event to the JSONL ledger.

        Serializes with deterministic key ordering. This is a best-effort
        operation — failures are logged but never raise exceptions.

        Args:
            event: Validated event dictionary.

        Returns:
            True if the event was appended successfully, False otherwise.
        """
        # Validate before writing
        errors = validate_event(event)
        if errors:
            logger.warning("Event validation failed, not appending: %s", errors)
            return False

        if not self._ensure_ledger_dir():
            return False

        try:
            line = canonicalize(event) + "\n"
            with open(self._ledger_file, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError as e:
            logger.warning("Failed to append event to ledger: %s", e)
            return False

    def emit(
        self,
        event_type: str,
        api_name: str,
        repository: str,
        version: str,
        commit: str,
        actor: str,
        spec_hash: str,
        diff_summary: List[Any],
        policy_result: str,
        complexity_score: int,
        complexity_class: str,
        timestamp: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Construct an event and append it to the ledger in one step.

        This is the primary API for event generation. It is best-effort:
        if the ledger write fails, the event is still returned but not persisted.

        CRITICAL: This method NEVER raises exceptions. Per Jamsons Doctrine,
        ledger failures must not affect CI pass/fail outcome.

        Returns:
            The event dictionary, or None if construction failed.
        """
        try:
            event = self.construct_event(
                event_type=event_type,
                api_name=api_name,
                repository=repository,
                version=version,
                commit=commit,
                actor=actor,
                spec_hash=spec_hash,
                diff_summary=diff_summary,
                policy_result=policy_result,
                complexity_score=complexity_score,
                complexity_class=complexity_class,
                timestamp=timestamp,
            )
        except ValueError as e:
            logger.warning("Event construction failed: %s", e)
            return None

        # Best-effort append — log warning on failure, never fatal
        success = self.append_event(event)
        if not success:
            logger.warning("Ledger append failed for event %s — CI continues normally",
                           event.get("event_hash", "unknown"))

        return event


def emit_pipeline_event(
    ledger_dir: Optional[str] = None,
    event_type: str = "contract_change",
    api_name: str = "",
    repository: str = "",
    version: str = "",
    commit: str = "",
    actor: str = "",
    spec_hash: str = "",
    diff_summary: Optional[List[Any]] = None,
    policy_result: str = "passed",
    complexity_score: int = 0,
    complexity_class: str = "simple",
) -> Optional[Dict[str, Any]]:
    """Convenience function for CI pipeline integration.

    Called after diff_engine → policy_engine → complexity_analyzer.
    Best-effort: never raises, never affects CI outcome.
    """
    backbone = EventBackbone(ledger_dir=ledger_dir)
    return backbone.emit(
        event_type=event_type,
        api_name=api_name,
        repository=repository,
        version=version,
        commit=commit,
        actor=actor,
        spec_hash=spec_hash,
        diff_summary=diff_summary if diff_summary is not None else [],
        policy_result=policy_result,
        complexity_score=complexity_score,
        complexity_class=complexity_class,
    )
