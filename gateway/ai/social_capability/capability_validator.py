"""Capability-currency validator (LED-216 Phase 1, tightened LED-1240).

Validates social drafts against ``current_capabilities.yaml``. The validator
hard-fails any draft that:

  * names a banned surface (literal or regex), OR
  * mentions the Delimit product without anchoring to ground truth (no
    canonical phrase AND no matched_claim from allowed_claims), OR
  * mentions the Delimit product on a long-form platform (reddit, hn, devto,
    etc.) without a delimit.ai URL anchor.

The 2026-05-05 tightening (LED-1240) was a response to founder feedback that
generic-claim drafts were leaking through with only a soft warning. The fix
reclassifies "mentions product, names no specific claim, links no artifact"
as a hard-fail — drafts that talk about Delimit must either (a) name a
mechanism from allowed_claims, or (b) link to a delimit.ai resource (the
methodology, a worked-example report, the attestation replay UI, etc.).

Twitter (and other ≤280-char platforms) gets a deliberate carve-out: a URL
won't always fit, so for ``platform="twitter"`` the URL requirement is
relaxed to "draft must contain at least one matched_claim". Reddit, HN,
devto, and any unspecified platform keep the URL requirement.

Wiring: ``ai.social.save_draft`` calls :func:`validate_draft` after the
existing tone/length checks but BEFORE the file is appended. On hard-fail
the draft's ``quality`` is overridden to ``"rejected_capability_drift"`` and
the entry MUST NOT be enqueued for notify. On warn the quality becomes
``"ready_with_warnings"``. Both outcomes are logged to
``~/.delimit/social_drafts_validation.jsonl`` for audit / replay.

Governance: the underlying ``current_capabilities.yaml`` is gated by the
LED-1037 banned-vocabulary contract — edits require a unanimous
``delimit_deliberate`` verdict. This module only consumes the file; it does
not mutate it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger("delimit.ai.social_capability.capability_validator")

# Default capabilities path — co-located with this module so the file ships
# (or doesn't) with the same npm exclusion rule as the rest of
# ai/social_capability/. The package name is `social_capability` rather
# than `social` because `ai/social.py` already exists as a top-level
# module and Python forbids a package with the same name as a sibling
# module.
DEFAULT_CAPABILITIES_PATH = Path(__file__).parent / "current_capabilities.yaml"

# Audit log for every validator decision (pass / warn / fail). One JSONL
# line per draft. Used by self-repair to detect over- or under-firing.
VALIDATION_LOG = Path.home() / ".delimit" / "social_drafts_validation.jsonl"

# ── product-mention detection ────────────────────────────────────────

# Case-insensitive match for "Delimit" as a standalone word OR the
# @delimit_ai twitter handle. Avoid false positives on hostnames like
# "delimit.ai/methodology/..." by NOT requiring word boundaries — the URL
# itself is a product reference, which is exactly the case where we want
# to nudge the canonical phrase.
_PRODUCT_MENTION_RE = re.compile(
    r"(?:\bDelimit\b|@delimit_ai|delimit\.ai)",
    re.IGNORECASE,
)

# Match a delimit.ai URL anchor — either a recognized known path, or a bare
# delimit.ai reference. Used for the LED-1240 long-form URL-grounding gate.
# The known-path list is recognised by the validator and any other
# delimit.ai/<segment> URL also counts as grounding (we only need to know
# the draft is anchored to a real artifact on the public site).
_DELIMIT_URL_RE = re.compile(
    r"\bdelimit\.ai(?:/(?:methodology|reports|att|docs|trust|pricing)\b|/\S+|\b)",
    re.IGNORECASE,
)

# Platforms that get the short-form URL relaxation. Anything not in this set
# is treated as long-form and must carry a delimit.ai URL anchor when the
# draft mentions Delimit by name.
_SHORT_FORM_PLATFORMS = {"twitter", "x"}


def _load_capabilities(path: Path) -> Dict[str, Any]:
    """Load and parse the capabilities YAML.

    Returns an empty config (no banned, no claims) if the file is missing
    or YAML is unavailable, so the validator fails open in degraded
    environments rather than crashing draft generation. The fail-open is
    intentional: a missing capability file should NOT block legitimate
    drafts; the worst outcome is the draft passes through without
    capability-currency enforcement, which we'll catch via the audit log.
    """
    if _yaml is None:
        logger.warning(
            "capability_validator: PyYAML not available; validator is a no-op"
        )
        return {}
    if not path.exists():
        logger.warning(
            "capability_validator: capabilities file missing at %s; "
            "validator is a no-op",
            path,
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
    except Exception as exc:  # pragma: no cover — corrupt yaml
        logger.error(
            "capability_validator: failed to load %s: %s", path, exc
        )
        return {}
    if not isinstance(data, dict):
        logger.error(
            "capability_validator: %s did not parse to a mapping", path
        )
        return {}
    return data


def _matched_claims(text: str, claims: List[Dict[str, Any]]) -> List[str]:
    """Return IDs of allowed_claims whose surface_name appears in text."""
    out: List[str] = []
    lower = text.lower()
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        surface = (claim.get("surface_name") or "").strip()
        cid = (claim.get("id") or "").strip()
        if not surface or not cid:
            continue
        if surface.lower() in lower:
            out.append(cid)
    return out


def _matched_banned_literal(
    text: str, banned: List[str]
) -> List[str]:
    """Return banned surface literals (case-insensitive) found in text."""
    lower = text.lower()
    return [b for b in banned if isinstance(b, str) and b and b.lower() in lower]


def _matched_banned_pattern(
    text: str, patterns: List[str]
) -> List[str]:
    """Return banned regex patterns that match somewhere in text.

    Compilation errors on individual patterns are logged and the pattern is
    skipped so one bad regex does not break the whole validator.
    """
    out: List[str] = []
    for pat in patterns:
        if not isinstance(pat, str) or not pat:
            continue
        try:
            if re.search(pat, text, flags=re.IGNORECASE):
                out.append(pat)
        except re.error as exc:
            logger.warning(
                "capability_validator: bad banned_surface_pattern %r: %s",
                pat, exc,
            )
            continue
    return out


def _has_canonical_phrase(text: str, phrases: List[str]) -> bool:
    lower = text.lower()
    for p in phrases:
        if isinstance(p, str) and p and p.lower() in lower:
            return True
    return False


def _mentions_product(text: str) -> bool:
    return bool(_PRODUCT_MENTION_RE.search(text or ""))


def _has_delimit_url(text: str) -> bool:
    """True iff the draft contains any delimit.ai URL anchor.

    Matches both the curated path list (delimit.ai/methodology, /reports,
    /att, /docs, /trust, /pricing) and any other delimit.ai/<path> URL.
    Bare 'delimit.ai' is also accepted — the goal is grounding, not exact
    path validation.
    """
    return bool(_DELIMIT_URL_RE.search(text or ""))


def _append_audit(record: Dict[str, Any]) -> None:
    """Append a validation decision to the audit log. Best-effort."""
    try:
        VALIDATION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(VALIDATION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # pragma: no cover — disk full, etc.
        logger.debug("capability_validator: audit write failed: %s", exc)


def validate_draft(
    text: str,
    capabilities_path: Optional[Path] = None,
    *,
    platform: str = "",
    audit_meta: Optional[Dict[str, Any]] = None,
    log: bool = True,
) -> Dict[str, Any]:
    """Validate a social draft against ``current_capabilities.yaml``.

    Args:
        text: The candidate draft text.
        capabilities_path: Override path to the capabilities YAML. Defaults
            to the bundled ``current_capabilities.yaml`` next to this
            module.
        platform: Platform string ("twitter", "reddit", "hn", "devto", ...).
            Twitter / X get a short-form carve-out: drafts that mention the
            product without a delimit.ai URL still pass IF they cite a
            specific allowed_claim. Long-form platforms must carry both a
            specific claim AND a URL anchor when they mention the product.
            Empty string defaults to long-form behavior (strictest gate).
        audit_meta: Optional fields to embed in the audit log entry
            (e.g. ``{"draft_id": ..., "platform": ...}``). Never required
            for validation logic.
        log: When False, skip the audit log write. Used by tests.

    Returns:
        Dict with:
          - ``ok`` (bool): False iff a banned surface (literal or pattern)
            appeared, OR the draft mentions the product but anchors to no
            ground truth (LED-1240).
          - ``errors`` (list[str]): Hard-fail reasons.
          - ``warnings`` (list[str]): Soft-fail reasons.
          - ``matched_claims`` (list[str]): IDs of allowed_claims found.
          - ``matched_banned`` (list[str]): Banned surfaces / patterns hit.
          - ``mentions_product`` (bool): Whether the draft references
            Delimit by name or handle.
          - ``has_canonical_phrase`` (bool): Whether at least one canonical
            phrase appears.
          - ``has_delimit_url`` (bool): Whether the draft contains a
            delimit.ai URL anchor.
          - ``platform`` (str): The normalized platform string used for the
            short-form carve-out decision.
    """
    text = text or ""
    path = capabilities_path or DEFAULT_CAPABILITIES_PATH
    cfg = _load_capabilities(path)

    allowed_claims = cfg.get("allowed_claims") or []
    banned_literals = cfg.get("banned_surfaces") or []
    banned_patterns = cfg.get("banned_surface_patterns") or []
    required_phrases = cfg.get("required_canonical_phrases") or []

    matched_claims = _matched_claims(text, allowed_claims)
    matched_literal = _matched_banned_literal(text, banned_literals)
    matched_patterns = _matched_banned_pattern(text, banned_patterns)
    matched_banned = matched_literal + matched_patterns

    errors: List[str] = []
    for hit in matched_literal:
        errors.append(
            f"banned surface literal: {hit!r} — see ai/social_capability/current_capabilities.yaml"
        )
    for pat in matched_patterns:
        errors.append(
            f"banned surface pattern matched: {pat!r} (tool-count hero "
            f"language is forbidden in social copy)"
        )

    mentions_product = _mentions_product(text)
    has_canonical = _has_canonical_phrase(text, required_phrases)
    has_url = _has_delimit_url(text)
    platform_norm = (platform or "").strip().lower()
    is_short_form = platform_norm in _SHORT_FORM_PLATFORMS

    warnings: List[str] = []

    # ── LED-1240 grounding gate ──────────────────────────────────────
    # Tightened 2026-05-05: a draft that mentions Delimit but anchors to
    # no ground truth is now a hard-fail, not a soft warning. The two
    # rules are:
    #   (a) Product mention + no canonical phrase + no matched_claim
    #       ⇒ hard-fail on every platform. The draft is naming the
    #       product without grounding to anything in the allow list.
    #   (b) Product mention on a long-form platform without a delimit.ai
    #       URL anchor ⇒ hard-fail. Twitter / X are exempt because a URL
    #       won't always fit in 280 chars; for them, a matched_claim is
    #       sufficient grounding.
    # The carve-out preserves the existing twitter draft contract while
    # raising the floor for reddit / HN / devto / etc.
    if mentions_product and required_phrases:
        if not has_canonical and not matched_claims:
            errors.append(
                "draft mentions Delimit but cites no canonical phrase and no "
                "specific allowed_claim. Anchor the claim to a mechanism in "
                "current_capabilities.yaml or rewrite without naming the "
                "product (LED-1240)."
            )
        elif not is_short_form and not has_url:
            errors.append(
                "draft mentions Delimit on a long-form platform "
                f"(platform={platform_norm or 'unspecified'}) without a "
                "delimit.ai URL anchor. Cite a specific artifact "
                "(delimit.ai/methodology, /reports, /att, ...) so the claim "
                "is verifiable (LED-1240)."
            )
        elif not has_canonical:
            # Has a matched_claim but still missing a canonical phrase —
            # downgrade to a warning (existing soft-fail behavior). The
            # claim is grounded; the framing isn't on-message yet.
            warnings.append(
                "draft mentions Delimit and cites a specific claim but does "
                "not include a canonical phrase (merge gate / signed, "
                "replayable attestation / AI-written code / AI-assisted "
                "merge). Founder review recommended."
            )

    ok = not errors

    result: Dict[str, Any] = {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "matched_claims": matched_claims,
        "matched_banned": matched_banned,
        "mentions_product": mentions_product,
        "has_canonical_phrase": has_canonical,
        "has_delimit_url": has_url,
        "platform": platform_norm,
    }

    if log:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "errors": errors,
            "warnings": warnings,
            "matched_claims": matched_claims,
            "matched_banned": matched_banned,
            "mentions_product": mentions_product,
            "has_canonical_phrase": has_canonical,
            "has_delimit_url": has_url,
            "platform": platform_norm,
            "text_len": len(text),
            "capabilities_path": str(path),
        }
        if audit_meta:
            # Don't let audit_meta clobber computed fields.
            for k, v in audit_meta.items():
                record.setdefault(k, v)
        _append_audit(record)

    return result


__all__ = ["validate_draft", "DEFAULT_CAPABILITIES_PATH", "VALIDATION_LOG"]
