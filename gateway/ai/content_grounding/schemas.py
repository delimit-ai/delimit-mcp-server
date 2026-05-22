"""
Typed schemas for the content grounding layer (LED-1084 Week 1, A5 + A6).

Design constraints (from 2026-04-24 adversarial rebuttal):

  - Atomic typed claims (not prose arrays). Each claim names its type,
    carries evidence_refs, has a visibility flag, and optionally names a
    versioned inference rule.
  - Hard bans through Week 2: comparative, adoption, customer, and
    roadmap claim types fail-closed unless the exact text is whitelisted.
    Aggregate claims require a structured numeric_evidence field linked
    to a commit SHA.
  - Evidence refs are strings in a documented namespace:
      LED-<id>       → ledger item
      sha256:<hex>   → content hash
      git:<sha>      → commit SHA on delimit-gateway
      attest:<id>    → attestation bundle ID (att_…)
      url:<https…>   → public URL (for outreach events)
    Unknown prefixes fail-closed.

Back-compat: consumers should import from `ai.content_grounding`
(re-export) rather than this internal module directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class ClaimType(str, Enum):
    """Atomic claim categories. New types require a schema+validator update."""
    FEATURE = "feature"            # "Delimit wrap emits a signed attestation"
    CAPABILITY = "capability"      # "The scanner detects 27 breaking-change types"
    INCIDENT = "incident"          # "LED-1076 caused 4 false positives"
    COMPARATIVE = "comparative"    # "Faster than openapi-diff" — BANNED unless whitelisted
    ADOPTION = "adoption"          # "Used by Harbor" — BANNED unless whitelisted
    CUSTOMER = "customer"          # "X paid Y for Z" — BANNED unless whitelisted
    AGGREGATE = "aggregate"        # "134 tests passing" — requires numeric_evidence
    ROADMAP = "roadmap"            # "We're building X next" — BANNED outright
    PROCESS = "process"            # "Advisory by default, flip to enforcing"

# Claim types that fail-closed during Week 1/2 hardening unless the exact
# `text` value is present in the canonical phrase whitelist (A6).
HARD_BANNED_CLAIM_TYPES: frozenset = frozenset({
    ClaimType.COMPARATIVE,
    ClaimType.ADOPTION,
    ClaimType.CUSTOMER,
    ClaimType.ROADMAP,
})


class Visibility(str, Enum):
    """Allowlist, not denylist (per consensus privacy model)."""
    PUBLIC = "public"      # safe to surface on delimit.ai / social / storyline
    INTERNAL = "internal"  # founder + ops only
    PRIVATE = "private"    # customer data, never leaves local ledger


class EventType(str, Enum):
    """Normalized event categories ingested from ledger/attestations/git."""
    FEATURE_SHIPPED = "feature_shipped"
    RELEASE = "release"
    INCIDENT = "incident"
    INCIDENT_RESOLVED = "incident_resolved"
    DECISION = "decision"
    OUTREACH_EVENT = "outreach_event"
    ATTESTATION = "attestation"
    COMMIT = "commit"


# ---------------------------------------------------------------------------
# EvidenceRef
# ---------------------------------------------------------------------------

_VALID_EVIDENCE_PREFIXES: tuple = ("LED-", "sha256:", "git:", "attest:", "url:")


@dataclass
class EvidenceRef:
    """A single evidence pointer. Deterministic namespace."""
    ref: str

    def validate(self) -> List[str]:
        errs: List[str] = []
        if not self.ref:
            errs.append("evidence_ref: empty string")
            return errs
        if not any(self.ref.startswith(p) for p in _VALID_EVIDENCE_PREFIXES):
            errs.append(
                f"evidence_ref: unknown prefix '{self.ref}'. "
                f"Must start with one of: {_VALID_EVIDENCE_PREFIXES}"
            )
        return errs


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    """A typed atomic assertion with evidence.

    Per A5, every claim is an object. Generators may only mention facts
    whose corresponding Claim has:
      - type in an allowed set for the target surface
      - non-empty evidence_refs
      - visibility compatible with the surface (public for outreach)
      - optional inference_rule (versioned) if derived rather than direct

    `text` is the exact phrasing allowed. Paraphrases are NOT permitted
    during Week 1/2 — caller must either reuse `text` verbatim or fail.
    """
    claim_id: str
    type: ClaimType
    text: str
    evidence_refs: List[str] = field(default_factory=list)
    visibility: Visibility = Visibility.INTERNAL
    inference_rule: Optional[str] = None
    inference_rule_version: Optional[str] = None
    numeric_evidence: Optional[Dict[str, Any]] = None  # for AGGREGATE claims

    def validate(self, whitelist: Optional[frozenset] = None) -> List[str]:
        """Return list of validation errors. Empty = valid."""
        errs: List[str] = []
        if not self.claim_id or not self.claim_id.startswith("CLM-"):
            errs.append(f"claim_id must start with 'CLM-', got '{self.claim_id}'")
        if not self.text or not self.text.strip():
            errs.append("text: empty")
        if not self.evidence_refs:
            errs.append("evidence_refs: at least one required")
        else:
            for ref in self.evidence_refs:
                errs.extend(EvidenceRef(ref).validate())

        # A6: hard-banned claim types require whitelist match on exact text
        if self.type in HARD_BANNED_CLAIM_TYPES:
            allowed = whitelist or frozenset()
            if self.text not in allowed:
                errs.append(
                    f"claim_type '{self.type.value}' is HARD-BANNED in "
                    f"Week 1/2 unless exact text is whitelisted. "
                    f"text='{self.text[:80]}...' not in whitelist (size={len(allowed)})"
                )

        # AGGREGATE claims need structured numeric evidence
        if self.type == ClaimType.AGGREGATE:
            if not self.numeric_evidence:
                errs.append(
                    "aggregate claim requires numeric_evidence dict with "
                    "{value, unit, commit_sha} at minimum"
                )
            else:
                for k in ("value", "commit_sha"):
                    if k not in self.numeric_evidence:
                        errs.append(f"numeric_evidence missing required key: '{k}'")

        # Inference rules must be versioned
        if self.inference_rule and not self.inference_rule_version:
            errs.append(
                "inference_rule requires inference_rule_version "
                "(deterministic, versioned, testable)"
            )

        return errs

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["visibility"] = self.visibility.value
        return d


# ---------------------------------------------------------------------------
# GroundedEvent
# ---------------------------------------------------------------------------

@dataclass
class GroundedEvent:
    """One normalized event in the grounding layer.

    Produced by ingesting ledger + attestations + git log. An event
    carries its own evidence chain and a list of Claims generators
    may reuse verbatim.
    """
    event_id: str
    type: EventType
    date: str  # ISO-8601
    venture: str  # delimit / domainvested / wirereport / livetube
    evidence_refs: List[str] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    visibility: Visibility = Visibility.INTERNAL
    source: Optional[str] = None  # where the event was ingested from
    raw: Optional[Dict[str, Any]] = None  # original record for audit

    def validate(self, whitelist: Optional[frozenset] = None) -> List[str]:
        errs: List[str] = []
        if not self.event_id:
            errs.append("event_id: empty")
        if not self.date:
            errs.append("date: empty")
        else:
            try:
                datetime.fromisoformat(self.date.replace("Z", "+00:00"))
            except ValueError:
                errs.append(f"date: not ISO-8601: {self.date}")
        if not self.venture:
            errs.append("venture: empty")
        if self.venture not in {"delimit", "domainvested", "wirereport", "livetube"}:
            errs.append(f"venture: unknown '{self.venture}'")
        if not self.evidence_refs:
            errs.append("evidence_refs: at least one required")
        else:
            for ref in self.evidence_refs:
                errs.extend(EvidenceRef(ref).validate())
        for claim in self.claims:
            claim_errs = claim.validate(whitelist=whitelist)
            errs.extend(f"claim {claim.claim_id}: {e}" for e in claim_errs)
        return errs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "date": self.date,
            "venture": self.venture,
            "evidence_refs": list(self.evidence_refs),
            "claims": [c.to_dict() for c in self.claims],
            "visibility": self.visibility.value,
            "source": self.source,
            "raw": self.raw,
        }


# ---------------------------------------------------------------------------
# GroundingIndex
# ---------------------------------------------------------------------------

@dataclass
class GroundingIndex:
    """A point-in-time snapshot of all grounded events for a venture.

    Produced by `build_grounding_index`. Generators consume a filtered
    view of this index (e.g. `events where visibility='public' and
    date within last 7 days`).
    """
    venture: str
    built_at: str  # ISO-8601
    events: List[GroundedEvent] = field(default_factory=list)
    feature_whitelist: List[str] = field(default_factory=list)
    canon_version: Optional[str] = None

    def validate(self, whitelist: Optional[frozenset] = None) -> List[str]:
        errs: List[str] = []
        if not self.venture:
            errs.append("venture: empty")
        if not self.built_at:
            errs.append("built_at: empty")
        else:
            try:
                datetime.fromisoformat(self.built_at.replace("Z", "+00:00"))
            except ValueError:
                errs.append(f"built_at: not ISO-8601: {self.built_at}")
        for event in self.events:
            event_errs = event.validate(whitelist=whitelist)
            errs.extend(f"event {event.event_id}: {e}" for e in event_errs)
        return errs

    def public_events(self) -> List[GroundedEvent]:
        """Events safe to surface publicly. Visibility is allowlist."""
        return [e for e in self.events if e.visibility == Visibility.PUBLIC]

    def events_by_type(self, event_type: EventType) -> List[GroundedEvent]:
        return [e for e in self.events if e.type == event_type]
