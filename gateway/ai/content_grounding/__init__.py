"""
Delimit content grounding layer — LED-1084 Week 1.

Purpose: normalize ledger entries, attestations, and git history into
evidence-backed `GroundedEvent` records with typed atomic `Claim`s.
Every downstream generator (blog, social drafter, storyline) consumes
this layer and MUST NOT fabricate claims that aren't backed by an
evidence_ref.

Architectural amendments (per 2026-04-24 adversarial rebuttal,
/home/delimit/delimit-private/strategy/CONTENT_GROUNDING_REBUTTAL_2026_04.md):

  A3. Week 1 is strictly NON-PUBLISHING. Publish endpoints are
      hard-disabled at the code level (see `_PUBLISH_DISABLED` below).
  A5. Claims are typed atomic objects with explicit evidence_refs,
      visibility, and optional versioned inference_rule.
  A6. Hard bans during Week 1/2: comparative, adoption, customer,
      aggregate, roadmap claims reject unless exact text whitelisted
      or (for aggregates) backed by structured numeric evidence.
  A9. Deterministic extraction gate: extract → classify → map to
      allowed claim IDs → reject on any unmatched/uncertain claim →
      persist audit record. All content passes through this gate.
  A10. One-strike kill semantics: any externally published ungrounded
      claim reverts ALL generators to manual-only mode.

This module never generates public content. It only produces the
grounded event + claim records that generators consume.
"""
from .schemas import (
    ClaimType,
    Visibility,
    EventType,
    EvidenceRef,
    Claim,
    GroundedEvent,
    GroundingIndex,
)
from .build import (
    build_grounding_index,
    load_grounded_events,
    validate_claims,
    persist_grounding_index,
)
from .consume import (
    GroundingBundle,
    fetch_grounding_bundle,
    build_allowed_claim_set,
    load_feature_whitelist,
    unreleased_feature_detector,
    score_draft_grounding,
)
from .features import (
    build_feature_set,
    build_and_persist_features,
    extract_mcp_tools,
    extract_cli_commands,
)
from .telemetry import (
    summarize as summarize_gate_telemetry,
    recent_samples as recent_gate_samples,
)

__all__ = [
    # schemas
    "ClaimType",
    "Visibility",
    "EventType",
    "EvidenceRef",
    "Claim",
    "GroundedEvent",
    "GroundingIndex",
    # build
    "build_grounding_index",
    "load_grounded_events",
    "validate_claims",
    "persist_grounding_index",
    # consume (Week 2)
    "GroundingBundle",
    "fetch_grounding_bundle",
    "build_allowed_claim_set",
    "load_feature_whitelist",
    "unreleased_feature_detector",
    "score_draft_grounding",
    # features whitelist builder (Week 2)
    "build_feature_set",
    "build_and_persist_features",
    "extract_mcp_tools",
    "extract_cli_commands",
    # telemetry (Week 2 → Week 3 bridge)
    "summarize_gate_telemetry",
    "recent_gate_samples",
]

# A3: publish paths are OFF. Any attempt to publish grounded content
# externally during Week 1 raises. Flip to True only after Week 2
# hardening (claim-type classifiers, implication detection) and explicit
# founder approval.
_PUBLISH_DISABLED = True
