"""Signal sensing layer (LED-877).

Physically separates observational signals from the ledger. Signals are a
deliberation corpus, not a task queue — they must never be pulled by
build_loop as work. Import from ai.sensing.signal_store for ingest/query.
"""

from ai.sensing.schema import Signal, ValidationError, normalize_url, fingerprint_of
from ai.sensing.signal_store import (
    ingest,
    query,
    dedup_check,
    age_out_to_warm,
    freeze_cold,
    promote_to_ledger,
    SIGNALS_DIR,
    HOT_WINDOW_DAYS,
    WARM_WINDOW_DAYS,
)

__all__ = [
    "Signal",
    "ValidationError",
    "normalize_url",
    "fingerprint_of",
    "ingest",
    "query",
    "dedup_check",
    "age_out_to_warm",
    "freeze_cold",
    "promote_to_ledger",
    "SIGNALS_DIR",
    "HOT_WINDOW_DAYS",
    "WARM_WINDOW_DAYS",
]
