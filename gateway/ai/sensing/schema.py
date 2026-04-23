"""Signal schema + validation (LED-877).

A signal is an observation, not a commitment. Schema enforces enough metadata
for deliberation to work with, rejects empty-identity rows at ingest (killing
the LED-876 ghost-engage-task class of bug at its source).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


class ValidationError(ValueError):
    """Raised when a signal fails schema validation on ingest."""


_UTM_RE = re.compile(r"^utm_")


def normalize_url(url: str) -> str:
    """Canonicalize URL: strip utm_* query params, fragment, trailing slash."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip()
    if not p.scheme:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query) if not _UTM_RE.match(k)]
    path = p.path.rstrip("/") or "/"
    cleaned = urlunparse(
        (p.scheme.lower(), p.netloc.lower(), path, "", urlencode(query), "")
    )
    return cleaned


def fingerprint_of(platform: str, canonical_url: str, author: str) -> str:
    """Stable dedup key for a signal."""
    raw = f"{(platform or '').lower()}|{normalize_url(canonical_url)}|{(author or '').lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Signal:
    """A sensed observation from an external platform.

    Mandatory: canonical_url AND (author OR content_snippet).
    Anything weaker than that is rejected at ingest because deliberation
    cannot draw useful conclusions from a row with no identity.
    """

    fingerprint: str
    platform: str
    canonical_url: str
    author: str = ""
    author_handle: str = ""
    content_snippet: str = ""
    posted_at: str = ""
    ingested_at: str = ""
    classification: str = "signal"
    relevance_score: float = 0.0
    themes: List[str] = field(default_factory=list)
    raw_ref: str = ""
    id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_and_normalize(raw: Dict[str, Any]) -> Signal:
    """Convert a raw target dict from social_target.py into a validated Signal.

    Raises ValidationError on missing mandatory fields so bugs surface loudly
    at ingest rather than producing empty-identity rows that pollute the
    corpus (the LED-876 failure mode).
    """
    platform = (raw.get("platform") or "").strip()
    canonical_url = normalize_url(raw.get("canonical_url") or raw.get("url") or "")
    author = (raw.get("author") or "").strip()
    content_snippet = (raw.get("content_snippet") or raw.get("title") or "").strip()[:500]

    if not canonical_url:
        raise ValidationError("canonical_url is required")
    if not author and not content_snippet:
        raise ValidationError("at least one of author or content_snippet is required")
    if not platform:
        raise ValidationError("platform is required")

    return Signal(
        fingerprint=fingerprint_of(platform, canonical_url, author),
        platform=platform,
        canonical_url=canonical_url,
        author=author,
        author_handle=(raw.get("author_handle") or "").strip(),
        content_snippet=content_snippet,
        posted_at=(raw.get("posted_at") or "").strip(),
        ingested_at="",  # filled by signal_store.ingest
        classification=(raw.get("classification") or "signal").strip(),
        relevance_score=float(raw.get("relevance_score") or 0.0),
        themes=list(raw.get("themes") or []),
        raw_ref=(raw.get("raw_ref") or raw.get("source_url") or canonical_url).strip(),
    )
