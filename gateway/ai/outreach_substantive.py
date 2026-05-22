"""Substantive-outreach payload, gate, and dispatch (LED-2214b).

Implements the autonomous-github-outreach architecture ratified by the
2026-05-11 deliberation (A1 + Codex payload amendment, B3 + Claude reg-O
target-side veto, C1 single-responsibility daemon). Transcript stored
privately.

The three SHIFT-1 holes this module closes:

* **Empty-payload dispatch** — the old generic ``outreach`` task type
  could be dispatched on a bare "engage user" target with no evidence
  anchor. Twenty-nine LEDs (LED-915–965) had to be bulk-cancelled in
  2026-05 because of this class of failure. The dataclass enforces
  required evidence fields at construction time, so empty-payload
  dispatch is structurally impossible.
* **Reg-O / banking veto** — a perfectly substantive bug report on a
  banking-fintech repo still violates SHIFT-1 (KYC would deanonymize
  the operating account). ``is_banking_adjacent`` runs at both the scanner layer
  (impossible-by-construction) and the submit-time gate (defense in
  depth) so a regulator-adjacent target never reaches dispatch and
  never reaches submission.
* **Covert commercial outreach** — even with a substantive technical
  anchor, the agent might leak "btw try delimit-cli". The content gate
  rejects forbidden phrases including our own product names, and
  requires at least one concrete technical anchor (commit hash, spec
  path, issue number, or CVE) before allowing submission.

Public surface:

* :class:`SubstantiveCandidate` — typed payload schema for dispatch.
* :func:`is_banking_adjacent` — reg-O / fintech / banking classifier.
* :func:`extract_technical_anchors` — anchor extraction for content gate.
* :func:`check_substantive_content` — content-shape gate.
* :func:`evaluate_substantive_payload` — composite gate (target then content).
* :func:`build_candidate_from_github_target` — scanner-level constructor.
* :func:`dispatch_substantive_outreach` — wraps :func:`dispatch_task`
  with task_type='outreach_substantive' and the typed payload.

Not part of this module: the daemon (:mod:`ai.outreach_loop_daemon`)
that ticks scanner → file ledger → dispatch.
"""

from __future__ import annotations

import json as _json
import logging
import os as _os
import re
import subprocess as _subprocess
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path as _Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.outreach_substantive")


# ---------------------------------------------------------------------------
# LED-2266: env-configurable thresholds for the outreach gate stack.
#
# Each defense layer has a default value chosen during initial deployment
# (PR #179 anti-spam, PR #180 engagement-floor). Operators can tune any
# of them via env var without code changes — useful for trying tighter
# thresholds on a new venture, or loosening when scanner yield is low.
#
# Defaults are conservative: they reproduce the PR-as-shipped behavior
# when no env var is set. The lookup helpers below are the single source
# of truth — module constants below resolve through them at import time
# so each threshold is documented in one place.
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read an int env var; fall back to `default` on missing/invalid.

    Enforces `minimum` (e.g. >=1 for caps) to reject zero/negative
    overrides that would silently disable a defense. Logs at WARNING
    when an override is applied OR rejected so operators can see what
    the engine is actually using.
    """
    raw = _os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "config: %s=%r is not an integer — using default=%d", name, raw, default,
        )
        return default
    if value < minimum:
        logger.warning(
            "config: %s=%d below floor %d — using default=%d",
            name, value, minimum, default,
        )
        return default
    if value != default:
        logger.warning("config: %s overridden default=%d -> %d", name, default, value)
    return value


# ---------------------------------------------------------------------------
# Constants — keep these auditable. Edits require panel deliberation per
# the CLAUDE.md SHIFT-1 constitutional binding.
# ---------------------------------------------------------------------------

PROPOSED_ACTIONS = ("comment", "issue", "pr")

# CLAUDE.md SHIFT-1 HARD VETO. KYC will deanonymize the operating account
# on any of these target classes regardless of brand cover, so the target
# never enters the dispatch queue. Keyword match runs over the repo name +
# description + topics; any hit blocks the target.
#
# Conservative by design — false positives cost zero (we just don't
# engage), false negatives risk constitutional violation.
BANKING_ADJACENT_KEYWORDS: Tuple[str, ...] = (
    # Direct
    "bank", "banking", "credit-union", "credit union",
    # Brokerage / capital markets
    "broker", "brokerage", "securities", "custodian", "custody",
    "clearinghouse", "clearing-house", "settlement",
    # Payments / cards
    "payment", "payments", "card-issuer", "card issuer", "issuer-processor",
    "acquirer", "merchant-acquirer", "interchange", "ach ", "swift ",
    # Lending
    "lender", "lending", "mortgage", "underwriting", "underwrite",
    # Insurance (reg-adjacent under McCarran-Ferguson)
    "insurance", "insurer", "reinsurer", "underwriter",
    # Crypto-fiat onramps (FinCEN-regulated MSBs)
    "msb", "money-services-business", "money services business",
    "onramp", "off-ramp", "fiat-onramp",
    # Wealth / advisors (RIA / IAR regulated)
    "wealth-management", "wealth management", "registered investment",
    "ria-firm", "broker-dealer", "broker dealer",
    # Compliance / AML / KYC vendors (likely reg-O downstream)
    "aml-platform", "kyc-platform", "kyc-provider", "kyc provider",
    "bsa-aml", "sanctions-screening", "ofac-screening",
    # Regulator-adjacent
    "regulator", "regulatory-reporting", "fr-y-9c", "call-report",
    "fdic", "occ-supervised", "frb-supervised", "finra", "sec-registered",
    # Reg-O specifically
    "reg-o", "regulation-o", "regulation o", "regulation-w",
    # Stablecoins / fintech with clear bank rails
    "stablecoin", "neobank", "challenger-bank", "core-banking",
    "core banking", "ledger-banking", "open-banking",
)

# Self-references and commercial phrasing the agent must never emit on
# a third-party repo. Per panel verdict + Codex amendment, we ban our
# own product names too — substantive contributions stand on technical
# merit alone, not on naming the upstream tool.
#
# Matching is case-insensitive, word-boundary aware where it matters
# (e.g. "delimit" must not flag "delimited" or "delimiter").
FORBIDDEN_PHRASES: Tuple[str, ...] = (
    # Commercial framing
    "we built", "we made", "we created", "we developed", "we ship",
    "our tool", "our product", "our cli", "our service", "our platform",
    "you should try", "you might try", "you may want to try",
    "you could try", "give it a try", "give us a try",
    "check out our", "have a look at our", "take a look at our",
    "btw try", "btw, try", "by the way try",
    # Generic non-substantive
    "thanks for the project", "great project", "love the project",
    "interesting project",
)

# Word-boundary product names. Ban "delimit" and "delimit-cli" as
# standalone tokens; don't false-positive on "delimited" or "delimiter".
FORBIDDEN_PRODUCT_TOKENS: Tuple[str, ...] = (
    "delimit", "delimit-cli", "delimit.ai", "delimitdev",
)

# Minimum content length below which a body cannot be substantive
# regardless of anchors. Calibrated to "two-sentence bug report".
MIN_BODY_LENGTH = 200

# Patterns for technical-anchor extraction. At least one must hit.
_COMMIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#\d{1,7}\b")
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_SPEC_PATH_RE = re.compile(
    r"(?:^|[\s`])(?:[A-Za-z0-9_\-/\.]+/)?(?:openapi|swagger|asyncapi)"
    r"[\w\-/]*\.(?:ya?ml|json)\b",
    re.IGNORECASE,
)
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`])[A-Za-z0-9_\-/.]+\.(?:py|ts|tsx|js|jsx|go|rs|java|"
    r"rb|c|cc|cpp|h|md|ya?ml|json|toml|proto)\b"
)


# ---------------------------------------------------------------------------
# Payload schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubstantiveCandidate:
    """Typed dispatch payload for substantive github outreach.

    The dataclass is ``frozen=True`` (immutable) and the constructor
    enforces every required field — there is no path to a partially
    populated ``SubstantiveCandidate``, which is the entire point of
    the Codex amendment to A1. The scanner builds one of these or
    nothing; the dispatcher refuses to fire on anything else.

    Fields:
        repo: ``owner/name`` of the target repository. Required.
        category: One of ``pain_thread``, ``adoption_lead``,
            ``competitor_user``, ``own_repo_activity``. Required.
        target_artifact: Canonical URL of the artifact we'd act on
            (the issue, the PR, the repo root, etc.). Required.
        evidence_refs: Non-empty list of concrete technical anchors
            extracted from the target — issue numbers, commit hashes,
            spec paths, CVE IDs. Empty list raises at construction.
        proposed_action: One of ``comment``, ``issue``, ``pr``.
        subcategory: Optional finer-grained label (e.g.
            ``openapi_spec``). Allowed to be empty.
        venture: Sourcing venture (e.g. ``delimit``). Default ``delimit``.
        fingerprint: Scanner fingerprint for idempotency. Optional.
    """

    repo: str
    category: str
    target_artifact: str
    evidence_refs: Tuple[str, ...]
    proposed_action: str
    subcategory: str = ""
    venture: str = "delimit"
    fingerprint: str = ""

    def __post_init__(self):
        # Mirror normal validate-on-construct ergonomics for a frozen
        # dataclass. We use object.__setattr__ only for normalisation
        # before validation; validation itself just raises.
        if not self.repo or "/" not in self.repo:
            raise ValueError(
                f"SubstantiveCandidate.repo must be 'owner/name', got {self.repo!r}"
            )
        if self.category not in {
            "pain_thread", "adoption_lead", "competitor_user", "own_repo_activity",
        }:
            raise ValueError(
                f"SubstantiveCandidate.category invalid: {self.category!r}"
            )
        if not self.target_artifact:
            raise ValueError("SubstantiveCandidate.target_artifact is required")
        if not self.evidence_refs:
            raise ValueError(
                "SubstantiveCandidate.evidence_refs cannot be empty — "
                "empty-payload dispatch is structurally forbidden (LED-2214b)"
            )
        if self.proposed_action not in PROPOSED_ACTIONS:
            raise ValueError(
                f"SubstantiveCandidate.proposed_action must be one of "
                f"{PROPOSED_ACTIONS}, got {self.proposed_action!r}"
            )
        # Coerce evidence_refs to a tuple if a list slipped in. (frozen
        # dataclasses don't auto-coerce; we go through object.__setattr__.)
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence_refs"] = list(self.evidence_refs)
        return d


# ---------------------------------------------------------------------------
# Reg-O / banking target-side veto
# ---------------------------------------------------------------------------


def is_banking_adjacent(target: Dict[str, Any]) -> Tuple[bool, str]:
    """Return ``(is_adjacent, matched_keyword)``.

    Scans a target dict for any banking / fintech / regulator-adjacent
    keyword across the fields the scanner emits today (``canonical_url``,
    ``rationale``, ``content_snippet``, and the optional ``repo_topics``
    + ``repo_description`` if present). Match is substring + case
    insensitive on the lowercased haystack.

    LED-2265: also checks the org/username portion of the canonical URL
    for typo-squat impersonation of known regulated entities (e.g.
    ``JPM0RCHASE`` for ``jpmorgan``, ``g0ldman`` for ``goldman``). The
    raw keyword pass above misses these because the user-facing string
    isn't a banking-noun; the impersonation IS the signal. Defense in
    depth — the substantive engagement path should never land on a
    spoofed-bank account regardless of the repo's content topic.

    The first-match-wins return makes the logged reason actionable
    ("matched 'broker-dealer' in repo_description" or "matched
    typosquat:jpmorgan in author=JPM0RCHASE"). Callers should treat any
    True return as a hard veto — no override path exists at the scanner
    layer, by design.
    """
    haystack_parts: List[str] = []
    for key in (
        "canonical_url", "rationale", "content_snippet",
        "repo_topics", "repo_description", "repo", "source_id",
    ):
        value = target.get(key)
        if isinstance(value, list):
            haystack_parts.extend(str(v) for v in value)
        elif value is not None:
            haystack_parts.append(str(value))
    haystack = " ".join(haystack_parts).lower()
    for kw in BANKING_ADJACENT_KEYWORDS:
        if kw in haystack:
            return True, kw

    # LED-2265: typo-squat impersonation of known regulated orgs.
    typosquat = _is_typosquat_impersonation(target)
    if typosquat:
        return True, f"typosquat:{typosquat}"

    return False, ""


# LED-2265: known-regulated-entity org names. Used by the typo-squat
# impersonation check below. Names are lowercased and stored without
# common suffixes (`-bank`, `-chase`, etc.). Conservative list — false
# positives cost zero (we just don't engage), false negatives risk
# substantive engagement with a malicious impersonator.
_KNOWN_REGULATED_ORGS: Tuple[str, ...] = (
    # Tier-1 US banks
    "jpmorgan", "jpmorganchase", "chase", "goldman", "goldmansachs",
    "morganstanley", "citi", "citigroup", "citibank",
    "bankofamerica", "bofa", "wellsfargo", "usbank", "pnc", "truist",
    "capitalone",
    # Foreign G-SIBs
    "hsbc", "barclays", "deutschebank", "credit-suisse", "creditsuisse",
    "ubs", "santander", "bnpparibas", "societegenerale", "ing", "lloyds",
    # US clearing / capital markets
    "blackrock", "vanguard", "fidelity", "schwab", "interactive-brokers",
    "interactivebrokers", "nyse", "nasdaq",
    # Crypto / fintech with bank rails
    "coinbase", "kraken", "circle", "tether", "binance",
    # Card networks
    "visa", "mastercard", "amex", "americanexpress",
    # Regulators
    "fdic", "occ", "frb", "federalreserve", "finra", "secgov",
)


# LED-2265: simple homoglyph map for digit-for-letter substitutions.
# Keys are digits commonly used as letter substitutes; values are the
# letter they impersonate. Asymmetric on purpose (we transform a
# candidate username INTO a likely impersonated name, then compare).
_HOMOGLYPH_DIGITS: Dict[str, str] = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
}


def _normalize_for_typosquat(name: str) -> str:
    """Lowercase + strip non-alphanumeric + map digits to letters via the
    homoglyph table. ``JPM0RCHASE`` → ``jpmorchase`` (after step 1) →
    ``jpmorchase`` (digits absent). Used both for the candidate org name
    and as the comparison target — but the comparison list is built
    from raw _KNOWN_REGULATED_ORGS (already letters only), so the
    homoglyph step does the work."""
    alphanum = re.sub(r"[^a-z0-9]", "", name.lower())
    return "".join(_HOMOGLYPH_DIGITS.get(c, c) for c in alphanum)


def _is_typosquat_impersonation(target: Dict[str, Any]) -> str:
    """Return the matched known-org name if the target's author/org/repo
    appears to impersonate a regulated entity via digit-for-letter
    substitution. Returns "" if no impersonation suspected.

    Checks BOTH the github username AND the repo-name segment. Real
    JPMorgan engagement would be ``jpmorganchase/<repo>`` — anything
    matching the impersonation pattern that ISN'T the canonical org is
    flagged.
    """
    # Collect the candidate name parts: author (github username) and the
    # owner/name segment of the canonical_url.
    candidates: List[str] = []
    author = target.get("author") or ""
    if isinstance(author, str) and author:
        candidates.append(author)
    url = target.get("canonical_url") or ""
    if isinstance(url, str) and url:
        m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)", url)
        if m:
            candidates.append(m.group(1))  # org/user
            candidates.append(m.group(2))  # repo name
    fp = target.get("fingerprint") or ""
    if isinstance(fp, str) and fp:
        m = re.match(r"^github:[^:]+:([^/:]+)(?:/([^:]+))?", fp)
        if m:
            candidates.append(m.group(1))
            if m.group(2):
                candidates.append(m.group(2))

    for cand in candidates:
        # Only digit-bearing candidates can be homoglyph typosquats.
        # A pure-letter username like ``goldman`` would either be the
        # legit org (caught by BANKING_ADJACENT_KEYWORDS keyword pass)
        # or some other case (e.g. ``goldman-recipes``) where we don't
        # have positive evidence of impersonation intent. Digits are
        # the disambiguator.
        if not any(c.isdigit() for c in cand):
            continue
        normalized = _normalize_for_typosquat(cand)
        if not normalized:
            continue
        for org in _KNOWN_REGULATED_ORGS:
            if org in normalized:
                return org

    return ""


# ---------------------------------------------------------------------------
# Technical-anchor extraction + content gate
# ---------------------------------------------------------------------------


def extract_technical_anchors(text: str) -> Dict[str, List[str]]:
    """Extract all technical anchors found in ``text``.

    Returns a dict with keys ``commits``, ``issues``, ``cves``,
    ``spec_paths``, ``file_paths``. Empty lists mean nothing of that
    type was found. A non-empty union across any key is sufficient to
    satisfy the substantive-content gate.

    Spec paths are matched explicitly (openapi/swagger/asyncapi) and
    are also captured by the broader file-path regex, but the spec
    list is the load-bearing signal for adoption-lead targets.
    """
    if not text:
        return {"commits": [], "issues": [], "cves": [], "spec_paths": [], "file_paths": []}
    return {
        "commits": _COMMIT_HASH_RE.findall(text),
        "issues": _ISSUE_REF_RE.findall(text),
        "cves": _CVE_RE.findall(text),
        "spec_paths": [m.strip("` ") for m in _SPEC_PATH_RE.findall(text)],
        "file_paths": [m.strip("` ") for m in _FILE_PATH_RE.findall(text)],
    }


def _hits_forbidden_product_token(text_lower: str) -> Optional[str]:
    """Return the first product token present as a word, else None."""
    for token in FORBIDDEN_PRODUCT_TOKENS:
        pattern = r"\b" + re.escape(token) + r"\b"
        if re.search(pattern, text_lower):
            return token
    return None


def check_substantive_content(
    body: str,
    proposed_action: str,
) -> Dict[str, Any]:
    """Validate a draft body against the SHIFT-1 content rules.

    Order of checks (load-bearing — do not reorder without panel
    deliberation):

      1. Type / length floor — empty or under-length bodies block.
      2. Forbidden product tokens — bans our own names (defends against
         "btw try delimit-cli" class).
      3. Forbidden commercial phrases — bans the broader "we built /
         our tool / you should try" class.
      4. Technical anchor — must have at least one commit hash, issue
         ref, CVE, spec path, or file path. Without an anchor the body
         is "thanks for the project" by definition.

    The function does NOT enforce target-side reg-O veto — that lives
    at :func:`is_banking_adjacent`, called separately by
    :func:`evaluate_substantive_payload`. Splitting them keeps the
    failure modes distinguishable in logs and ledger entries.

    Returns:
        Dict with keys ``verdict`` (``"allow"`` | ``"block"``),
        ``reason``, ``violations`` (list of strings), ``anchors``
        (the extracted-anchors dict).
    """
    violations: List[str] = []
    if not isinstance(body, str) or not body.strip():
        return {
            "verdict": "block",
            "reason": "empty_body",
            "violations": ["body is empty"],
            "anchors": {},
        }
    if proposed_action not in PROPOSED_ACTIONS:
        return {
            "verdict": "block",
            "reason": "invalid_proposed_action",
            "violations": [f"proposed_action must be one of {PROPOSED_ACTIONS}"],
            "anchors": {},
        }
    if len(body) < MIN_BODY_LENGTH:
        violations.append(
            f"body length {len(body)} < MIN_BODY_LENGTH={MIN_BODY_LENGTH}"
        )

    body_lower = body.lower()
    product_hit = _hits_forbidden_product_token(body_lower)
    if product_hit:
        violations.append(f"forbidden_product_token: {product_hit!r}")
    for phrase in FORBIDDEN_PHRASES:
        if phrase in body_lower:
            violations.append(f"forbidden_phrase: {phrase!r}")

    anchors = extract_technical_anchors(body)
    has_anchor = any(anchors[k] for k in anchors)
    if not has_anchor:
        violations.append(
            "no_technical_anchor: body must cite a commit hash, "
            "issue number, CVE, spec path, or source file path"
        )

    if violations:
        return {
            "verdict": "block",
            "reason": violations[0].split(":")[0],
            "violations": violations,
            "anchors": anchors,
        }
    return {
        "verdict": "allow",
        "reason": "ok",
        "violations": [],
        "anchors": anchors,
    }


# ---------------------------------------------------------------------------
# Composite gate: target-side veto BEFORE content
# ---------------------------------------------------------------------------


def evaluate_substantive_payload(
    body: str,
    proposed_action: str,
    target: Optional[Dict[str, Any]] = None,
    repo: str = "",
    repo_description: str = "",
    repo_topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Full pre-submit gate: reg-O target veto, then content shape.

    Per the 2026-05-11 panel verdict + Claude's reg-O target-side veto
    amendment: target classification is checked FIRST. A perfectly
    substantive bug report on a banking-adjacent repo still violates
    SHIFT-1, so the gate refuses regardless of content quality.

    Callers can pass either:
      * a full ``target`` dict (forwarded to :func:`is_banking_adjacent`),
      * or the discrete ``repo`` / ``repo_description`` / ``repo_topics``
        fields, which we wrap in a synthetic target.

    Returns:
        Dict with ``verdict``, ``reason``, ``violations``, ``anchors``,
        and ``stage`` (``"target"`` or ``"content"``) indicating where
        the gate fired.
    """
    if target is None:
        target = {
            "repo": repo,
            "repo_description": repo_description,
            "repo_topics": repo_topics or [],
        }
    elif repo or repo_description or repo_topics:
        # Caller passed both — merge, keyword scan looks at union.
        target = {
            **target,
            **({"repo": repo} if repo else {}),
            **({"repo_description": repo_description} if repo_description else {}),
            **({"repo_topics": repo_topics} if repo_topics else {}),
        }

    adjacent, matched = is_banking_adjacent(target)
    if adjacent:
        return {
            "verdict": "block",
            "reason": "banking_adjacent_target",
            "violations": [f"banking_adjacent_target: matched keyword {matched!r}"],
            "anchors": {},
            "stage": "target",
        }

    content_result = check_substantive_content(body, proposed_action)
    content_result["stage"] = "content"
    return content_result


# ---------------------------------------------------------------------------
# Scanner-level constructor
# ---------------------------------------------------------------------------


_FINGERPRINT_REPO_RE = re.compile(
    r"^github:(?:issue|repo|fork|star|outreach):([^:]+/[^:]+)(?::|$)"
)
_URL_REPO_RE = re.compile(
    r"^https?://github\.com/([^/]+/[^/]+?)(?:/|$|#|\?)"
)


def _repo_from_target(target: Dict[str, Any]) -> str:
    repo = (target.get("repo") or "").strip()
    if repo and "/" in repo:
        return repo
    fingerprint = target.get("fingerprint", "")
    m = _FINGERPRINT_REPO_RE.match(fingerprint)
    if m:
        return m.group(1)
    url = target.get("canonical_url", "")
    m = _URL_REPO_RE.match(url)
    if m:
        return m.group(1)
    return ""


_CATEGORY_TO_ACTION = {
    "pain_thread": "comment",
    "adoption_lead": "issue",
    "competitor_user": "comment",
    "own_repo_activity": "comment",
}


# ---------------------------------------------------------------------------
# Issue-body fetch + cache (LED-2214b followup)
#
# The scanner truncates issue bodies to 200 chars before they reach the
# substantive gate (see ai/social_target.py:_scan_github phase 2). 200
# chars covers the title + opening summary but almost always strips the
# tail where anchors live — stack traces, file paths in error messages,
# references to other issues/commits. Result: every issue target gets
# rejected as no-anchor even when the issue body is anchor-rich.
#
# This block fetches the FULL issue body + first N comments via gh CLI
# when the snippet-derived extraction comes up empty. Per-issue 7-day
# disk cache; daily tick at max_dispatch=3 means worst-case ~3 API calls
# per day after cache warms.
# ---------------------------------------------------------------------------

_ISSUE_BODY_CACHE_DIR = _Path.home() / ".delimit" / "cache" / "outreach_issue_bodies"
# LED-2266: env-overridable via DELIMIT_OUTREACH_ISSUE_BODY_CACHE_TTL_S.
# Default 7 days. Minimum 60s (don't disable caching outright; would
# spam the github api on every tick).
_ISSUE_BODY_CACHE_TTL_S = _env_int(
    "DELIMIT_OUTREACH_ISSUE_BODY_CACHE_TTL_S", 7 * 24 * 3600, minimum=60,
)
_ISSUE_COMMENTS_FETCH_LIMIT = 5
_GH_API_TIMEOUT_S = 30
_ISSUE_FP_RE = re.compile(r"^github:issue:([^/:]+/[^/:]+):(\d+)$")


def _issue_fp_parts(fingerprint: str) -> Optional[Tuple[str, int]]:
    """Extract (repo, issue_number) from a ``github:issue:owner/name:N`` fp.

    Returns None for any non-issue fingerprint, so callers can use the
    None return as the "skip body fetch" signal.
    """
    m = _ISSUE_FP_RE.match(fingerprint or "")
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2))
    except (TypeError, ValueError):
        return None


def _issue_cache_path(repo: str, number: int) -> _Path:
    safe = repo.replace("/", "__")
    return _ISSUE_BODY_CACHE_DIR / f"{safe}_{number}.json"


def _read_cached_issue_body(repo: str, number: int) -> Optional[str]:
    """Return cached full-text or None if missing/expired/corrupt."""
    cache_file = _issue_cache_path(repo, number)
    if not cache_file.exists():
        return None
    try:
        data = _json.loads(cache_file.read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or _time.time() - ts > _ISSUE_BODY_CACHE_TTL_S:
        return None
    body = data.get("body")
    return body if isinstance(body, str) else None


def _write_cached_issue_body(repo: str, number: int, body: str) -> None:
    """Persist fetched body. Best-effort — silent on disk failure."""
    try:
        _ISSUE_BODY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _issue_cache_path(repo, number).write_text(
            _json.dumps({"ts": _time.time(), "body": body})
        )
    except OSError as exc:
        logger.warning(
            "issue-body cache write failed for %s#%d: %s", repo, number, exc,
        )


_RATE_LIMIT_KILL_FILE = _Path.home() / ".delimit" / "outreach_pause"
_RATE_LIMIT_SIGNATURES = (
    "rate limit", "rate-limit", "secondary rate",
    "403", "abuse detection", "too many requests",
)


def _maybe_halt_on_rate_limit(endpoint: str, stderr: str) -> None:
    """LED-2214b followup — defensive halt when github signals rate
    limit / abuse-detection / forbidden. Writes the kill-switch file
    AND ntfys (priority=5). The daemon's pre-import kill-switch check
    will then short-circuit subsequent ticks until the file is removed.

    Best-effort: silent on any failure. The halt is defense in depth —
    if it doesn't fire here, the rate limit's own retry-after backoff
    handles the immediate request, but future ticks would still hit
    the same limit. The halt-on-warning pattern protects the account
    from escalation (warning -> hard block -> ban)."""
    if not stderr:
        return
    sl = stderr.lower()
    if not any(sig in sl for sig in _RATE_LIMIT_SIGNATURES):
        return
    try:
        _RATE_LIMIT_KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RATE_LIMIT_KILL_FILE.write_text(
            f"halted by _maybe_halt_on_rate_limit at "
            f"{_time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())}\n"
            f"endpoint: {endpoint}\n"
            f"stderr: {stderr[:400]}\n"
        )
        logger.error(
            "outreach RATE LIMIT detected — wrote kill-switch %s "
            "(endpoint=%s)", _RATE_LIMIT_KILL_FILE, endpoint,
        )
    except OSError as exc:
        logger.error(
            "outreach rate-limit halt failed to write kill-switch: %s", exc,
        )


def _gh_api_call(endpoint: str) -> Any:
    """Call ``gh api <endpoint>`` and return parsed JSON or None on failure.

    Local copy of the same idiom in ai.social_target — duplicated to keep
    this module importable without pulling in the much larger
    social_target dependency graph.

    On any 403 / 429 / rate-limit signature in stderr, writes the
    kill-switch file so subsequent daemon ticks short-circuit. See
    _maybe_halt_on_rate_limit.
    """
    try:
        proc = _subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=_GH_API_TIMEOUT_S,
        )
    except (_subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("gh api %s failed: %s", endpoint, exc)
        return None
    if proc.returncode != 0:
        # LED-2214b followup: halt the outreach daemon on rate-limit
        # signatures BEFORE returning. Defense in depth against escalating
        # github enforcement (warn -> block -> ban).
        _maybe_halt_on_rate_limit(endpoint, proc.stderr or "")
        logger.info(
            "gh api %s returned %d: %s",
            endpoint, proc.returncode, (proc.stderr or "")[:160],
        )
        return None
    try:
        return _json.loads(proc.stdout)
    except ValueError as exc:
        logger.warning("gh api %s returned non-JSON: %s", endpoint, exc)
        return None


# ---------------------------------------------------------------------------
# Engagement-floor check (LED-2214b followup, found 2026-05-17 when first
# autonomous engagement landed on a same-day-created 0-star 4-follower
# personal scratchpad). Substantive content gate passed (anchors were
# valid) but engagement value was near zero — no readership, no community.
#
# This block fetches lightweight repo metadata (1 gh api call, 7-day
# cached) and enforces a stars + age + not-archived + not-fork floor
# BEFORE the anchor check. Sits parallel to the existing repo-search
# filter in ai/social_target.py:_scan_github line 2024 ("stars == 0 and
# no description: continue") which only catches REPO targets — issue
# targets bypass it entirely, which was the gap.
#
# Fail-closed: if we can't fetch the metadata, we DON'T engage. Better
# to skip a real target than spam a maintainer on stale / missing data.
# ---------------------------------------------------------------------------

_REPO_META_CACHE_DIR = _Path.home() / ".delimit" / "cache" / "outreach_repo_meta"
# LED-2266: env-overridable engagement-floor thresholds.
# Defaults reproduce PR #180 shipped behavior. Floors enforce sanity
# (no zero or negative values that would silently disable the gate).
_REPO_META_CACHE_TTL_S = _env_int(
    "DELIMIT_OUTREACH_REPO_META_CACHE_TTL_S", 7 * 24 * 3600, minimum=60,
)
_MIN_REPO_STARS = _env_int("DELIMIT_OUTREACH_MIN_STARS", 50, minimum=1)
_MIN_REPO_AGE_DAYS = _env_int("DELIMIT_OUTREACH_MIN_AGE_DAYS", 30, minimum=1)


def _repo_meta_cache_path(repo: str) -> _Path:
    safe = repo.replace("/", "__")
    return _REPO_META_CACHE_DIR / f"{safe}.json"


def _read_cached_repo_meta(repo: str) -> Optional[Dict[str, Any]]:
    cache_file = _repo_meta_cache_path(repo)
    if not cache_file.exists():
        return None
    try:
        data = _json.loads(cache_file.read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("_cached_ts")
    if not isinstance(ts, (int, float)) or _time.time() - ts > _REPO_META_CACHE_TTL_S:
        return None
    meta = data.get("meta")
    return meta if isinstance(meta, dict) else None


def _write_cached_repo_meta(repo: str, meta: Dict[str, Any]) -> None:
    try:
        _REPO_META_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _repo_meta_cache_path(repo).write_text(
            _json.dumps({"_cached_ts": _time.time(), "meta": meta})
        )
    except OSError as exc:
        logger.warning("repo-meta cache write failed for %s: %s", repo, exc)


def fetch_repo_metadata(repo: str) -> Optional[Dict[str, Any]]:
    """Fetch lightweight repo metadata via ``gh api repos/{repo}``.
    Cached 7 days. Returns dict with stargazers_count / forks_count /
    open_issues_count / created_at / archived / fork / description /
    pushed_at / owner_login, or None on any failure (caller fails closed)."""
    cached = _read_cached_repo_meta(repo)
    if cached is not None:
        return cached
    data = _gh_api_call(f"repos/{repo}")
    if not isinstance(data, dict):
        # Don't poison cache with None — repo may exist on next attempt
        return None
    owner_obj = data.get("owner") or {}
    meta = {
        "stargazers_count": data.get("stargazers_count", 0),
        "forks_count": data.get("forks_count", 0),
        "open_issues_count": data.get("open_issues_count", 0),
        "created_at": data.get("created_at", ""),
        "pushed_at": data.get("pushed_at", ""),
        "archived": bool(data.get("archived", False)),
        "fork": bool(data.get("fork", False)),
        "description": data.get("description") or "",
        # LED-2214b followup: owner login lets the engagement-floor veto
        # owner-authored issues / PRs. Most owner-authored items are
        # internal chore/release artifacts (today's audit queue had 4 of
        # 5 real candidates in this class) — engagement value near zero.
        "owner_login": owner_obj.get("login", "") if isinstance(owner_obj, dict) else "",
    }
    _write_cached_repo_meta(repo, meta)
    return meta


# LED-2214b followup: per-issue state cache. Lighter than fetch_issue_full_text
# (which pulls body + comments) — we only need the state field. Separate cache
# because issue state changes more often than repo metadata, so shorter TTL.
_ISSUE_STATE_CACHE_TTL_S = 6 * 3600  # 6h: catches "open then closed same day"


def _issue_state_cache_path(repo: str, number: int) -> _Path:
    safe = repo.replace("/", "__")
    return _ISSUE_BODY_CACHE_DIR / f"{safe}_{number}__state.json"


def _read_cached_issue_state(repo: str, number: int) -> Optional[str]:
    cf = _issue_state_cache_path(repo, number)
    if not cf.exists():
        return None
    try:
        data = _json.loads(cf.read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("_cached_ts")
    if not isinstance(ts, (int, float)) or _time.time() - ts > _ISSUE_STATE_CACHE_TTL_S:
        return None
    state = data.get("state")
    return state if isinstance(state, str) else None


def _write_cached_issue_state(repo: str, number: int, state: str) -> None:
    try:
        _ISSUE_BODY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _issue_state_cache_path(repo, number).write_text(
            _json.dumps({"_cached_ts": _time.time(), "state": state})
        )
    except OSError as exc:
        logger.warning(
            "issue-state cache write failed for %s#%d: %s", repo, number, exc,
        )


def fetch_issue_state(repo: str, number: int) -> Optional[str]:
    """Return current github issue/PR state ('open' / 'closed') or None
    on fetch failure. Cached 6h. Fail-closed: callers treating None as
    'don't engage' is correct (we can't verify the target is live)."""
    cached = _read_cached_issue_state(repo, number)
    if cached is not None:
        return cached
    data = _gh_api_call(f"repos/{repo}/issues/{number}")
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    if isinstance(state, str) and state:
        _write_cached_issue_state(repo, number, state)
        return state
    return None


def _repo_age_days(created_at: str) -> Optional[float]:
    """Parse ISO timestamp and return age in days. None on parse failure."""
    if not created_at:
        return None
    try:
        # Strip fractional seconds + Z suffix
        clean = created_at.replace("Z", "").split(".")[0]
        epoch = _time.mktime(_time.strptime(clean, "%Y-%m-%dT%H:%M:%S")) - _time.timezone
    except (ValueError, TypeError):
        return None
    return (_time.time() - epoch) / 86400.0


def check_engagement_floor(repo: str) -> Tuple[bool, str]:
    """Apply the engagement-worthiness floor.

    Returns (passes, reason). On failure, reason is a short tag the
    caller logs: ``stars<50:3`` / ``age_days<30:0.4`` / ``archived`` /
    ``fork`` / ``no_metadata``. Tunable thresholds: _MIN_REPO_STARS,
    _MIN_REPO_AGE_DAYS.
    """
    meta = fetch_repo_metadata(repo)
    if meta is None:
        return False, "no_metadata"
    if meta.get("archived"):
        return False, "archived"
    if meta.get("fork"):
        return False, "fork"
    stars = meta.get("stargazers_count", 0) or 0
    if stars < _MIN_REPO_STARS:
        return False, f"stars<{_MIN_REPO_STARS}:{stars}"
    age = _repo_age_days(meta.get("created_at", ""))
    if age is not None and age < _MIN_REPO_AGE_DAYS:
        return False, f"age_days<{_MIN_REPO_AGE_DAYS}:{age:.1f}"
    return True, "ok"


def fetch_issue_full_text(repo: str, number: int) -> str:
    """Fetch issue body + first N comments concatenated.

    Cached for 7 days. Returns "" on any failure — the caller treats
    empty string as 'no anchors available' which correctly blocks
    dispatch (defense in depth; we never accidentally dispatch on a
    target whose substantive evidence we couldn't actually fetch).

    Public surface (no underscore prefix) so tests + callers can
    monkeypatch without depending on the private cache helpers.
    """
    cached = _read_cached_issue_body(repo, number)
    if cached is not None:
        return cached

    issue = _gh_api_call(f"repos/{repo}/issues/{number}")
    if not isinstance(issue, dict):
        _write_cached_issue_body(repo, number, "")
        return ""
    parts: List[str] = []
    body = issue.get("body")
    if isinstance(body, str) and body:
        parts.append(body)

    comments = _gh_api_call(
        f"repos/{repo}/issues/{number}/comments?per_page={_ISSUE_COMMENTS_FETCH_LIMIT}"
    )
    if isinstance(comments, list):
        for c in comments[:_ISSUE_COMMENTS_FETCH_LIMIT]:
            if isinstance(c, dict):
                cb = c.get("body")
                if isinstance(cb, str) and cb:
                    parts.append(cb)

    full = "\n\n".join(parts)
    _write_cached_issue_body(repo, number, full)
    return full


# ---------------------------------------------------------------------------
# Anti-spam — protect the operating account from github enforcement
# ---------------------------------------------------------------------------
#
# Three hard limits on top of the per-tick spam firewall
# (DEFAULT_MAX_DISPATCH=3) in the daemon:
#
#   1. Per-repo cooldown: don't dispatch on a repo we already dispatched
#      to within the last _DISPATCH_COOLDOWN_DAYS days. Avoids the
#      "scanner finds 3 issues on the SAME repo in one tick + we
#      engage on all of them = swarm" failure mode.
#   2. Per-day global cap: refuse dispatch once we've crossed
#      _MAX_DISPATCHES_PER_DAY in the rolling 24-hour window. Catches
#      multiple-tick scenarios (manual run + scheduled run + retry)
#      that would multiply the per-tick cap.
#   3. Halt on rate-limit (in _gh_api_call): if gh api returns 403/429,
#      write the kill-switch file and ntfy. GitHub typically warns
#      before banning; respecting that warning protects the account.
#
# The dispatch log at _DISPATCH_LOG is the source of truth for #1 and #2.
# It's append-only JSONL; each successful dispatch_substantive_outreach
# call writes one line.

_DISPATCH_LOG = _Path.home() / ".delimit" / "state" / "outreach-dispatch-log.jsonl"
# LED-2266: env-overridable anti-spam thresholds (PR #179 follow-up
# panel-flagged). Defaults reproduce shipped behavior. Floors enforce
# sanity (minimum=1 — zero would silently disable the spam protection).
_DISPATCH_COOLDOWN_DAYS = _env_int("DELIMIT_OUTREACH_COOLDOWN_DAYS", 7, minimum=1)
_MAX_DISPATCHES_PER_DAY = _env_int("DELIMIT_OUTREACH_MAX_PER_DAY", 5, minimum=1)


def _read_dispatch_log() -> List[Dict[str, Any]]:
    """Return all dispatch log entries (newest first). Empty on missing/
    unreadable. Best-effort — never raises."""
    if not _DISPATCH_LOG.exists():
        return []
    try:
        out: List[Dict[str, Any]] = []
        for line in _DISPATCH_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except ValueError:
                continue
        out.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return out
    except OSError as exc:
        logger.warning("dispatch log read failed: %s", exc)
        return []


def _record_dispatch(repo: str, fingerprint: str, category: str) -> None:
    """Append one entry to the dispatch log. Best-effort — silent on
    disk failure (dispatch must not crash because logging broke)."""
    try:
        _DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "repo": repo,
            "fingerprint": fingerprint,
            "category": category,
        }
        with _DISPATCH_LOG.open("a") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("dispatch log write failed: %s", exc)


def _check_per_repo_cooldown(repo: str, now: float | None = None) -> Optional[str]:
    """Return cooldown-expiry ISO string if repo is in cooldown, else None.

    `now` is overridable for tests. Defaults to current UTC epoch.
    """
    if not repo:
        return None
    if now is None:
        now = _time.time()
    cutoff = now - (_DISPATCH_COOLDOWN_DAYS * 86400)
    for entry in _read_dispatch_log():
        if (entry.get("repo") or "").strip().lower() != repo.strip().lower():
            continue
        ts = entry.get("ts", "")
        try:
            entry_epoch = _time.mktime(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - _time.timezone
        except (ValueError, TypeError):
            continue
        if entry_epoch >= cutoff:
            # Compute cooldown-expiry as entry_ts + cooldown_days
            expires_epoch = entry_epoch + (_DISPATCH_COOLDOWN_DAYS * 86400)
            return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(expires_epoch))
    return None


def _check_per_day_cap(now: float | None = None) -> int:
    """Return count of dispatches in the rolling 24h window. Caller
    checks against _MAX_DISPATCHES_PER_DAY."""
    if now is None:
        now = _time.time()
    cutoff = now - 86400
    count = 0
    for entry in _read_dispatch_log():
        ts = entry.get("ts", "")
        try:
            entry_epoch = _time.mktime(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - _time.timezone
        except (ValueError, TypeError):
            continue
        if entry_epoch >= cutoff:
            count += 1
    return count


def build_candidate_from_github_target(
    target: Dict[str, Any],
    category: str,
    subcategory: str = "",
) -> Optional[SubstantiveCandidate]:
    """Build a :class:`SubstantiveCandidate` or return None.

    The function returns None — *not* raises — when the target cannot
    yield a substantive payload. This is the structural-impossibility
    guarantee: callers that get None must NOT dispatch.

    Reasons for None return:
      * Target classified banking-adjacent (SHIFT-1 hard veto).
      * Repo could not be derived from fingerprint or URL.
      * No technical anchor extractable from snippet + rationale.
      * Category not in the mapped action table.

    The reg-O check happens here too, not just at submit time, so
    banking-adjacent targets never reach the agent prompt at all.
    Defense in depth: scanner + submit gate both veto.
    """
    adjacent, matched = is_banking_adjacent(target)
    if adjacent:
        logger.info(
            "build_candidate: banking-adjacent veto fingerprint=%s matched=%s",
            target.get("fingerprint"), matched,
        )
        return None

    repo = _repo_from_target(target)
    if not repo:
        logger.info(
            "build_candidate: repo unresolved fingerprint=%s url=%s",
            target.get("fingerprint"), target.get("canonical_url"),
        )
        return None

    if category not in _CATEGORY_TO_ACTION:
        logger.info("build_candidate: unmapped category=%s", category)
        return None

    # LED-2214b followup (founder's Niklas-Flaig observation 2026-05-17):
    # engagement-floor check BEFORE the anchor extraction + body fetch so
    # we don't pay the per-issue API cost on a target that's a 0-star
    # personal scratchpad. Existing repo-search filter in social_target
    # catches `stars==0 AND no description` for repo targets only; issue
    # targets bypassed it entirely (the gap this closes).
    floor_ok, floor_reason = check_engagement_floor(repo)
    if not floor_ok:
        logger.info(
            "build_candidate: engagement floor fingerprint=%s repo=%s reason=%s",
            target.get("fingerprint"), repo, floor_reason,
        )
        return None

    # LED-2214b followup (2026-05-17 audit-queue observation): 4 of 7
    # dispatched tasks today were owner-authored (chore PRs, dev→main
    # promotions, internal scout reports). Engagement value near zero —
    # the owner is doing their own work, not seeking community input.
    # Repo metadata fetch above already populated owner_login; compare
    # directly to target's author. Cheap check.
    repo_meta = fetch_repo_metadata(repo)
    if repo_meta is not None:
        owner_login = (repo_meta.get("owner_login") or "").strip().lower()
        target_author = (target.get("author") or "").strip().lower()
        if owner_login and target_author and owner_login == target_author:
            logger.info(
                "build_candidate: owner-authored target fingerprint=%s "
                "author=%s == owner=%s",
                target.get("fingerprint"), target_author, owner_login,
            )
            return None

    # LED-2214b followup (2026-05-17 audit-queue observation): 3 of 7
    # dispatched tasks today were on CLOSED issues. Engaging on a closed
    # thread is noise — the decision is already made. Cheap state check
    # before paying the body-fetch cost. Only applies to issue targets;
    # repo targets don't have a state in this sense.
    fp_parts_state = _issue_fp_parts(target.get("fingerprint", ""))
    if fp_parts_state is not None:
        state = fetch_issue_state(fp_parts_state[0], fp_parts_state[1])
        if state is None:
            # Fail-closed: can't verify the issue is live → skip
            logger.info(
                "build_candidate: issue state unverifiable fingerprint=%s",
                target.get("fingerprint"),
            )
            return None
        if state != "open":
            logger.info(
                "build_candidate: issue state=%s (not open) fingerprint=%s",
                state, target.get("fingerprint"),
            )
            return None

    # LED-2214b followup — anti-spam protection for the operating account.
    # These checks run AFTER the banking veto + repo-resolve + category
    # check (so we don't burden the dispatch log with rejected targets
    # that wouldn't have dispatched anyway) but BEFORE the anchor
    # extraction + body fetch (so cool-down catches re-targeting on
    # repos we recently engaged with without paying the API cost to
    # re-fetch their issue body).

    cooldown_expires = _check_per_repo_cooldown(repo)
    if cooldown_expires:
        logger.info(
            "build_candidate: per-repo cooldown fingerprint=%s repo=%s "
            "expires=%s",
            target.get("fingerprint"), repo, cooldown_expires,
        )
        return None

    today_count = _check_per_day_cap()
    if today_count >= _MAX_DISPATCHES_PER_DAY:
        logger.warning(
            "build_candidate: per-day cap hit fingerprint=%s "
            "today_count=%d cap=%d",
            target.get("fingerprint"), today_count, _MAX_DISPATCHES_PER_DAY,
        )
        return None

    snippet = target.get("content_snippet", "") or ""
    rationale = target.get("rationale", "") or ""
    anchors = extract_technical_anchors(f"{snippet}\n{rationale}")

    # LED-2214b followup: if the snippet didn't yield anchors AND this is
    # an issue target, fetch the full issue body + first N comments and
    # re-extract. The scanner truncates issue bodies to 200 chars (see
    # ai/social_target.py:_scan_github phase 2) which almost always
    # strips the part where anchors live. Fetch is cached 7 days per
    # issue (see fetch_issue_full_text). On any fetch failure the
    # function returns "" which leaves anchors unchanged → still blocks.
    fp_parts = _issue_fp_parts(target.get("fingerprint", ""))
    needs_body_fetch = fp_parts is not None and not any(
        anchors.get(k) for k in ("issues", "spec_paths", "cves", "commits", "file_paths")
    )
    if needs_body_fetch:
        body = fetch_issue_full_text(fp_parts[0], fp_parts[1])
        if body:
            anchors = extract_technical_anchors(
                f"{snippet}\n{rationale}\n{body}"
            )

    evidence_refs: List[str] = []
    for key in ("issues", "spec_paths", "cves", "commits", "file_paths"):
        for ref in anchors.get(key, []):
            label = f"{key[:-1] if key.endswith('s') else key}:{ref}"
            if label not in evidence_refs:
                evidence_refs.append(label)
    if not evidence_refs:
        logger.info(
            "build_candidate: no_technical_anchor fingerprint=%s category=%s "
            "(body_fetched=%s)",
            target.get("fingerprint"), category, needs_body_fetch,
        )
        return None

    target_artifact = target.get("canonical_url") or target.get("fingerprint", "")
    if not target_artifact:
        return None

    try:
        return SubstantiveCandidate(
            repo=repo,
            category=category,
            target_artifact=target_artifact,
            evidence_refs=tuple(evidence_refs),
            proposed_action=_CATEGORY_TO_ACTION[category],
            subcategory=subcategory or "",
            venture=target.get("venture", "delimit"),
            fingerprint=target.get("fingerprint", "") or "",
        )
    except ValueError as exc:
        logger.warning(
            "build_candidate: construction failed for fingerprint=%s: %s",
            target.get("fingerprint"), exc,
        )
        return None


# ---------------------------------------------------------------------------
# Dispatch wrapper
# ---------------------------------------------------------------------------


OUTREACH_SUBSTANTIVE_TASK_TYPE = "outreach_substantive"


def dispatch_substantive_outreach(
    candidate: SubstantiveCandidate,
    target: Dict[str, Any],
    ledger_item_id: str = "",
) -> Dict[str, Any]:
    """Dispatch a substantive outreach task — only fires on a real payload.

    The payload is the :class:`SubstantiveCandidate` — its construction
    has already enforced that every required evidence field is present.
    The task_type ``outreach_substantive`` is distinct from the legacy
    ``outreach`` type (which still serves reddit / x branches) so a
    regression that tries to dispatch a non-substantive github task on
    the old type does not silently route to the new agent.

    The agent that picks up this task is expected to call
    ``delimit_substantive_content_check`` BEFORE submitting any draft
    body, and ``delimit_external_pr_check`` BEFORE submitting if the
    action is ``pr``. Those gates live in :mod:`ai.server`.
    """
    if not isinstance(candidate, SubstantiveCandidate):
        # Belt-and-suspenders: the dataclass cannot be constructed
        # without the required fields, but a caller might still pass
        # a stray dict. Refuse rather than coerce.
        raise TypeError(
            "dispatch_substantive_outreach requires a SubstantiveCandidate "
            f"instance, got {type(candidate).__name__}"
        )

    # Late-bound import to keep the foundation module light and the
    # cyclic-import surface clean.
    from ai.agent_dispatch import dispatch_task, link_ledger_item

    constraints = [
        "no-deploy", "no-secrets", "no-destructive",
        "shift-1-quiet-attraction",
        "must-call-delimit_substantive_content_check-before-submit",
    ]
    if candidate.proposed_action == "pr":
        constraints.append("must-call-delimit_external_pr_check-before-submit")

    tools_needed = [
        "delimit_substantive_content_check",
        "delimit_sensor_github_issue",
    ]
    if candidate.proposed_action == "pr":
        tools_needed.append("delimit_external_pr_check")

    variables: Dict[str, Any] = {
        "candidate": candidate.to_dict(),
        "venture": candidate.venture,
        "repo": candidate.repo,
        "category": candidate.category,
        "subcategory": candidate.subcategory,
        "target_artifact": candidate.target_artifact,
        "evidence_refs": list(candidate.evidence_refs),
        "proposed_action": candidate.proposed_action,
        "source_url": target.get("canonical_url", ""),
        "source_fingerprint": candidate.fingerprint,
        "author": target.get("author", ""),
        "rationale": target.get("rationale", ""),
    }

    title = (
        f"[{candidate.venture.upper()}] Substantive {candidate.proposed_action} "
        f"on {candidate.repo} ({candidate.category})"
    )

    description = (
        "Substantive-outreach task (LED-2214b architecture).\n"
        f"Repo: {candidate.repo}\n"
        f"Category: {candidate.category}"
        f"{' / ' + candidate.subcategory if candidate.subcategory else ''}\n"
        f"Action: {candidate.proposed_action}\n"
        f"Target: {candidate.target_artifact}\n"
        f"Evidence: {', '.join(candidate.evidence_refs)}\n"
        "\n"
        "SHIFT-1 constraints:\n"
        " - Pseudonymous account only; no founder identity.\n"
        " - Real technical contribution only. No 'we built' / 'our tool' / "
        "'btw try' framing. Never name our own product in the body.\n"
        " - delimit_substantive_content_check is MANDATORY pre-submit.\n"
        " - delimit_external_pr_check is MANDATORY when proposed_action='pr'.\n"
    )

    context = (
        "Substantive autonomous outreach via the LED-2214b architecture. "
        "The pseudonymous-substantive-contribution carve-out (CLAUDE.md SHIFT-1, "
        "2026-05-04) permits this provided the activity is a genuine technical "
        "contribution. The pre-submit gate stack enforces that. If the gate "
        "blocks, file the rejection reason on the linked ledger item and stop."
    )

    result = dispatch_task(
        title=title,
        description=description,
        assignee="any",
        priority="P1",
        tools_needed=tools_needed,
        constraints=constraints,
        context=context,
        task_type=OUTREACH_SUBSTANTIVE_TASK_TYPE,
        venture=candidate.venture,
        variables=variables,
        external_key=(
            f"outreach_substantive:{candidate.fingerprint}"
            if candidate.fingerprint
            else f"outreach_substantive:{candidate.repo}:{candidate.target_artifact}"
        ),
    )
    task_id = result.get("task_id", "")
    if task_id and ledger_item_id:
        try:
            link_ledger_item(task_id, ledger_item_id)
        except Exception as exc:  # link is best-effort
            logger.warning(
                "dispatch_substantive_outreach: link_ledger_item failed "
                "task=%s ledger=%s err=%s",
                task_id, ledger_item_id, exc,
            )

    # LED-2214b followup — record the dispatch for per-repo cooldown +
    # per-day cap. Append-only JSONL; subsequent build_candidate calls
    # read this log via _check_per_repo_cooldown / _check_per_day_cap.
    # Best-effort; logging failures must not crash a successful dispatch.
    if task_id:
        _record_dispatch(
            repo=candidate.repo,
            fingerprint=candidate.fingerprint,
            category=candidate.category,
        )

    return result
