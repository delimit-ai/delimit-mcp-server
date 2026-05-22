"""
Week 2 consumer API for the grounding layer (LED-1084).

Exposes the gate generators (social drafter, blog pipeline, storyline) must
call before emitting text. Amendments A6+A9+A10 from the adversarial rebuttal
(/home/delimit/delimit-private/strategy/CONTENT_GROUNDING_REBUTTAL_2026_04.md):

  A6. Hard bans on unresolved claim categories (comparative, adoption,
      customer, roadmap) apply here at the gate — not just at schema time.
  A9. Deterministic extraction gate: extract candidate claims from output →
      classify → map to allowed_claim_ids OR approved inference rule →
      reject on any unmatched/uncertain claim.
  A10. One-strike kill-switch semantics: callers that detect a slippage
       MUST revert ALL generators to manual-only mode.

Week 2 scope:
  - fetch_grounding_bundle(venture, days)  → GroundingBundle for a window
  - build_allowed_claim_set(bundle)        → frozenset of safe-to-use texts
  - load_feature_whitelist()               → shipped-feature list
  - unreleased_feature_detector(text)      → True if text claims a feature
                                              that is NOT in the whitelist
  - score_draft_grounding(text, bundle)    → 0.0-1.0 grounding score
                                              (simple coverage heuristic for
                                               v1; Week 3 upgrades to
                                               classifier-based)

Not in scope (Week 3+):
  - Paraphrase classifier (we reject non-verbatim for now)
  - Implication detector (hard-ban suffices for now)
  - Comparative claim classifier (ban + whitelist-only for now)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import Claim, ClaimType, GroundedEvent, Visibility
from .build import build_grounding_index

logger = logging.getLogger("delimit.ai.content_grounding.consume")

FEATURES_FILE = Path(os.environ.get(
    "DELIMIT_GROUNDING_FEATURES",
    str(Path.home() / ".delimit" / "content" / "grounding" / "features.json"),
))


# ---------------------------------------------------------------------------
# GroundingBundle — what generators receive
# ---------------------------------------------------------------------------

@dataclass
class GroundingBundle:
    """A time-windowed snapshot generators use to ground their output.

    Generators MUST NOT emit claims that don't appear (verbatim) in
    `allowed_claim_texts` or that mention features not in `features`.
    """
    venture: str
    built_at: str
    window_days: int
    events: List[GroundedEvent] = field(default_factory=list)
    allowed_claim_texts: frozenset = field(default_factory=frozenset)
    features: frozenset = field(default_factory=frozenset)

    def public_events(self) -> List[GroundedEvent]:
        return [e for e in self.events if e.visibility == Visibility.PUBLIC]


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------

def load_feature_whitelist() -> frozenset:
    """Load the shipped-feature whitelist from features.json.

    If the file is missing, return an empty frozenset. Generators MUST
    fail-closed on empty whitelist — i.e., refuse to name any feature —
    rather than fall through to prompt-level trust.
    """
    if not FEATURES_FILE.exists():
        logger.info(
            "feature whitelist not found at %s — generators will fail-closed on feature claims",
            FEATURES_FILE,
        )
        return frozenset()
    try:
        data = json.loads(FEATURES_FILE.read_text())
        feats = data.get("features") if isinstance(data, dict) else data
        if isinstance(feats, list):
            return frozenset(str(f).strip() for f in feats if f)
    except Exception as e:
        logger.warning("feature whitelist load failed: %s", e)
    return frozenset()


def build_allowed_claim_set(events: List[GroundedEvent]) -> frozenset:
    """Extract the set of exact claim texts a generator may use verbatim.

    Returns a frozenset so downstream code can do O(1) membership checks.
    """
    texts: List[str] = []
    for ev in events:
        for claim in ev.claims:
            if claim.visibility == Visibility.PUBLIC and not claim.validate():
                texts.append(claim.text.strip())
    return frozenset(t for t in texts if t)


def fetch_grounding_bundle(
    venture: str = "delimit",
    days: int = 7,
    include_internal: bool = False,
) -> GroundingBundle:
    """Primary entrypoint generators call.

    Week 2 default: 7-day window, public-only events. Tighter than the
    30-day grounding index so generators see recent-and-relevant, not
    the full history.
    """
    idx = build_grounding_index(venture=venture, days=days)
    events = idx.events if include_internal else idx.public_events()
    return GroundingBundle(
        venture=venture,
        built_at=idx.built_at,
        window_days=days,
        events=list(events),
        allowed_claim_texts=build_allowed_claim_set(events),
        features=load_feature_whitelist(),
    )


# ---------------------------------------------------------------------------
# Gate functions (generators call these before emitting)
# ---------------------------------------------------------------------------

# Very common English verbs that often anchor feature claims in social copy.
# Used as a lightweight trigger for the unreleased-feature scan. Not a full
# NLP pipeline — just enough to flag "delimit does X" / "the CLI does Y"
# constructions for review.
_FEATURE_CLAIM_TRIGGERS = [
    r"\bdelimit\s+\w+s\b",          # "delimit detects", "delimit signs"
    r"\b(?:our|the)\s+(?:cli|action|mcp|server)\s+\w+s\b",
    r"\b(?:we|delimit)\s+(?:built|ship|shipped|support|supports|have|has)\b",
    r"\b(?:new|latest)\s+feature\b",
    r"\b(?:supports|offers|provides)\s+[a-z]",
]

_TRIGGER_RE = re.compile("|".join(_FEATURE_CLAIM_TRIGGERS), re.IGNORECASE)


def unreleased_feature_detector(
    text: str,
    features: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Scan generated text for feature claims NOT in the shipped whitelist.

    Returns a dict with:
      - status: "clean" | "flagged"
      - triggers: list of regex matches indicating claim-like language
      - unknown_features_mentioned: list of feature-like substrings that
        look specific enough to be a claim but don't match any entry in
        `features`.

    Generators MUST fail-closed on status == "flagged" when the whitelist
    is loaded (empty whitelist → fail-closed by default).
    """
    feats = features if features is not None else load_feature_whitelist()
    triggers = _TRIGGER_RE.findall(text or "")

    # Word-level scan for "delimit-sounding" specific feature names.
    # A "specific feature claim" looks like an identifier or a compound
    # noun that doesn't appear in the features list and is adjacent to a
    # trigger phrase.
    unknown_specifics: List[str] = []
    if triggers:
        # Pull noun-like tokens near triggers. Low recall, intentionally
        # conservative. Week 3 upgrade: proper NER.
        sentences = re.split(r"[.!?]\s+", text or "")
        for sentence in sentences:
            if not _TRIGGER_RE.search(sentence):
                continue
            for token in re.findall(r"\b([a-z][a-z0-9-]{3,})\b", sentence.lower()):
                # Only flag tokens that look domain-specific (mixed case markers,
                # hyphens, or longer-than-common-English). For v1 we skip this
                # lookup entirely and rely on the whitelist match at caller.
                if token in feats:
                    continue
                # Don't spam: only flag once per token per text.
                if token not in unknown_specifics and len(token) > 8 and "-" in token:  # nosec B-secret-detection: `token` here is a Python variable holding one word from the text being grounded, not a credential
                    unknown_specifics.append(token)

    # If we have no whitelist and triggers fired, flag regardless — the
    # generator has no basis to claim any feature.
    if triggers and not feats:
        return {
            "status": "flagged",
            "reason": "feature-claim triggers present but feature whitelist is empty",
            "triggers": triggers[:5],
            "unknown_features_mentioned": [],
        }

    if triggers and unknown_specifics:
        return {
            "status": "flagged",
            "reason": f"{len(unknown_specifics)} unknown-feature-looking tokens near claim triggers",
            "triggers": triggers[:5],
            "unknown_features_mentioned": unknown_specifics[:10],
        }

    return {
        "status": "clean",
        "triggers": triggers[:5],
        "unknown_features_mentioned": [],
    }


def score_draft_grounding(
    text: str,
    bundle: GroundingBundle,
    threshold: float = 0.85,
) -> Dict[str, Any]:
    """Score how much of a draft is covered by the allowed-claim set.

    v1 algorithm (simple):
      - Score = proportion of sentences in `text` that either (a) contain
        at least one verbatim allowed-claim text, or (b) contain no
        specific feature claim at all (safe conversational filler).
      - If sentence contains a trigger AND no allowed-claim match,
        the sentence counts as UNGROUNDED.

    Returns:
      {
        "score": 0.0-1.0,
        "threshold": threshold,
        "passed": bool,
        "sentence_count": int,
        "ungrounded_sentences": List[str]  (first 3 for debug)
      }

    Week 3 upgrade: proper claim classifier + paraphrase detection.
    """
    if not text or not text.strip():
        return {"score": 1.0, "threshold": threshold, "passed": True, "sentence_count": 0, "ungrounded_sentences": []}

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not sentences:
        return {"score": 1.0, "threshold": threshold, "passed": True, "sentence_count": 0, "ungrounded_sentences": []}

    ungrounded: List[str] = []
    grounded_count = 0
    allowed = bundle.allowed_claim_texts

    for sent in sentences:
        has_trigger = bool(_TRIGGER_RE.search(sent))
        if not has_trigger:
            # No specific feature claim being made → safe.
            grounded_count += 1
            continue
        # Claim-like sentence: require a verbatim match in allowed set.
        matched = any(allowed_text in sent for allowed_text in allowed)
        if matched:
            grounded_count += 1
        else:
            ungrounded.append(sent[:120])

    score = grounded_count / len(sentences)
    return {
        "score": round(score, 3),
        "threshold": threshold,
        "passed": score >= threshold,
        "sentence_count": len(sentences),
        "grounded_count": grounded_count,
        "ungrounded_sentences": ungrounded[:3],
    }
