"""
Self-repair deliberation layer (deliberate-mode).

Wraps the gateway's `delimit_deliberate` engine with the diagnostic
bundle as context, classifies the panel's recommended fix into a tier,
runs a mandatory escalation pre-flight check, and renders the verdict
as a founder-approval email.

This layer ends at "verdict emailed; founder approves." There is NO
auto-apply, NO fix execution. The fix-application gate is the next
layer (apply-mode).

Constraints (per /home/delimit/delimit-private/strategy/PROPOSED_SELF_REPAIR_LOOP.md
v3 + the 2026-04-30 panel verdict):

  - All fixes require founder approval in v1 — `requires_founder_approval`
    is True for every verdict regardless of the panel's confidence.
  - Pre-flight escalation hard-stops cannot be bypassed by the panel.
    If the recommended fix matches a hard-stop keyword sourced from
    `social_outreach.yaml::escalation_class_hard_stops`, the verdict is
    flagged and the email subject prefix becomes `[self-repair-ESCALATION]`.
  - 300-second hard timeout. Caller falls through to diagnose-mode email
    if deliberation does not return in time.

Public API:
    run_deliberation(breach, bundle, function_yaml, deliberate_fn=None)
        -> DeliberationVerdict
    render_verdict_email(verdict) -> tuple[str, str]   # (subject, body)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .diagnose import DiagnosticBundle, render_json
from .kpi import Breach

logger = logging.getLogger("delimit.ai.self_repair.deliberate")

# Where transcripts are saved. One subdirectory per function so the
# knowledge base stays browsable.
HISTORY_DIR = Path.home() / ".delimit" / "self_repair_history"

# Default hard timeout for one panel deliberation. Caller treats a
# timeout as "fall through to diagnose-mode email" — we never want to
# silently drop a breach.
DEFAULT_DELIBERATION_TIMEOUT_SECONDS = 300

# Fix tiers (per PROPOSED_SELF_REPAIR_LOOP.md auto-apply matrix).
# `code_change` is the most-restrictive default — when in doubt the loop
# treats the fix as a code change so the founder explicitly approves.
FIX_TIERS = (
    "prompt_rewrite",
    "kpi_adjust",
    "disable_temp",
    "code_change",
    "scope_expansion",
    "spend_increase",
)
DEFAULT_FIX_TIER = "code_change"

# Escalation hard-stop keywords. These mirror the yaml-declared list at
# `social_outreach.yaml::escalation_class_hard_stops` plus a small set
# of natural-language variants the panel might emit. Each entry maps
# the canonical hard-stop name to a tuple of regex patterns that
# detect it in panel-emitted text.
_HARD_STOP_PATTERNS: Dict[str, Tuple[re.Pattern[str], ...]] = {
    "force_push_to_main": (
        re.compile(r"\bforce[-\s]?push(?:ing)?\b", re.IGNORECASE),
        re.compile(r"\bgit\s+push\s+(?:--force|-f)\b", re.IGNORECASE),
    ),
    "ruleset_bypass": (
        re.compile(r"\bruleset[-\s]bypass(?:ing)?\b", re.IGNORECASE),
        re.compile(r"\bdisable\s+(?:the\s+)?ruleset\b", re.IGNORECASE),
        re.compile(r"\bbypass(?:ing)?\s+(?:the\s+)?ruleset\b", re.IGNORECASE),
    ),
    "branch_protection_bypass": (
        re.compile(r"\bbranch[-\s]protection[-\s](?:bypass|disable)\b", re.IGNORECASE),
        re.compile(r"\bbypass(?:ing)?\s+branch\s+protection\b", re.IGNORECASE),
        re.compile(r"\bdisable\s+branch\s+protection\b", re.IGNORECASE),
    ),
    "account_switch": (
        re.compile(r"\baccount[-\s]switch(?:ing)?\b", re.IGNORECASE),
        re.compile(r"\bswitch\s+to\s+(?:the\s+)?crypttrx\b", re.IGNORECASE),
        re.compile(r"\binfracore\s*(?:↔|<->|->|/)\s*crypttrx\b", re.IGNORECASE),
    ),
    "irreversible_capital_commit": (
        re.compile(r"\birreversible[-\s]capital(?:[-\s]commit)?\b", re.IGNORECASE),
        re.compile(r"\birreversible\s+(?:spend|payment|purchase|commit)\b", re.IGNORECASE),
    ),
    "scope_expansion_beyond_stated_function": (
        re.compile(
            r"\bscope[-\s]expansion(?:\s+beyond\s+(?:the\s+)?(?:stated\s+)?function)?\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bexpand\s+(?:the\s+)?(?:function'?s?\s+)?scope\b", re.IGNORECASE),
    ),
    "mcp_tool_signature_change": (
        re.compile(r"\bmcp[-\s]tool[-\s]signature(?:[-\s]change)?\b", re.IGNORECASE),
        re.compile(
            r"\b(?:rename|remove|change)\s+(?:an?\s+)?mcp\s+tool\b", re.IGNORECASE
        ),
        re.compile(
            r"\bchange\s+(?:the\s+)?mcp\s+tool\s+signature\b", re.IGNORECASE
        ),
    ),
    "cli_command_rename_or_remove": (
        re.compile(
            r"\bcli[-\s]command[-\s](?:rename|removal|remove)\b", re.IGNORECASE
        ),
        re.compile(
            r"\b(?:rename|remove)\s+(?:a\s+|the\s+)?cli\s+command\b", re.IGNORECASE
        ),
    ),
    "storage_format_change": (
        re.compile(r"\bstorage[-\s]format[-\s]change\b", re.IGNORECASE),
        re.compile(
            r"\bchange\s+(?:the\s+)?(?:jsonl|storage|on[-\s]disk)\s+format\b",
            re.IGNORECASE,
        ),
    ),
    "clobber_user_customized_files": (
        re.compile(r"\bclobber(?:ing)?\s+user[-\s]customized\s+files?\b", re.IGNORECASE),
        re.compile(r"\boverwrite\s+/root/CLAUDE\.md\b", re.IGNORECASE),
        re.compile(r"\boverwrite\s+\.claude/settings\.json\b", re.IGNORECASE),
    ),
}


# ── data model ───────────────────────────────────────────────────────


@dataclass
class DeliberationVerdict:
    """Structured panel verdict + fix proposal for one breach.

    `requires_founder_approval` is True for every verdict in v1. The
    field exists so post-graduation versions can flip it to False on
    auto-applicable tiers without changing the data model.
    """

    breach: Breach
    bundle: DiagnosticBundle
    timestamp: str
    status: str  # 'unanimous' | 'split' | 'error'
    rounds: int
    final_verdict: str
    proposed_fix: Dict[str, Any] = field(default_factory=dict)
    escalation_class: List[str] = field(default_factory=list)
    transcript_path: str = ""
    requires_founder_approval: bool = True
    raw_panel_response: Dict[str, Any] = field(default_factory=dict)


# ── deliberate_fn resolution ─────────────────────────────────────────


def _default_deliberate_fn(
    *,
    question: str,
    context: str,
    max_rounds: int = 3,
    mode: str = "debate",
    scope: str = "operational",
) -> Dict[str, Any]:
    """Resolve and call the gateway's deliberation engine.

    The MCP tool surface is `delimit_deliberate`; the underlying Python
    entry-point is `ai.deliberation.deliberate(...)`. We call it
    directly so the watcher works headless.

    If the import fails (stripped-down test env or broken install) we
    surface the error in the verdict rather than crashing — the watcher
    falls through to diagnose-mode email so the founder still hears
    about the breach.
    """
    try:
        from ai.deliberation import deliberate  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time fallback
        logger.warning(
            "self_repair: cannot import ai.deliberation.deliberate (%s) — "
            "deliberation disabled for this pass",
            exc,
        )
        return {
            "error": f"deliberation engine unavailable: {exc}",
            "final_verdict": "ERROR",
            "status": "error",
            "rounds": [],
        }

    return deliberate(
        question=question,
        context=context,
        max_rounds=max_rounds,
        mode=mode,
        scope=scope,
    )


# ── prompt construction ──────────────────────────────────────────────


_QUESTION_TEMPLATE = (
    "Function {function} is failing KPI {kpi_name} (severity {severity}, "
    "{actual} vs {threshold}). Diagnostic bundle attached. What is the "
    "smallest reversible change to recover the KPI? Classify the proposed "
    "fix into one of: prompt_rewrite, kpi_adjust, disable_temp, "
    "code_change, scope_expansion, spend_increase. Reject any fix that "
    "requires force-push, ruleset bypass, account switch, irreversible "
    "capital, branch-protection bypass, MCP signature change, CLI rename, "
    "storage format change, or clobbering user-customized files."
)


def _build_question(breach: Breach) -> str:
    return _QUESTION_TEMPLATE.format(
        function=breach.function,
        kpi_name=breach.kpi_name,
        severity=breach.severity,
        actual=breach.actual,
        threshold=breach.threshold,
    )


def _build_context(bundle: DiagnosticBundle) -> str:
    """Render the diagnostic bundle as JSON for the panel context.

    JSON is preferred over the plain-text render because models parse
    it more reliably and we want structured fields (trend deltas,
    baseline last-known-good window, etc.) to survive intact.
    """
    payload = render_json(bundle)
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


# ── transcript persistence ───────────────────────────────────────────


def _safe_segment(value: str) -> str:
    """Sanitize a string for use as a file path segment."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return cleaned or "unknown"


def _save_transcript(
    *,
    function: str,
    kpi_name: str,
    timestamp: str,
    raw_response: Dict[str, Any],
    history_dir: Optional[Path] = None,
) -> str:
    """Write the panel transcript to
    `~/.delimit/self_repair_history/<function>/<timestamp>-<kpi>.md`.

    Format is markdown with a JSON code block — readable in a browser
    or terminal, and trivially round-trippable for the future
    knowledge-base loader.
    """
    base = history_dir or HISTORY_DIR
    fn_dir = base / _safe_segment(function)
    fn_dir.mkdir(parents=True, exist_ok=True)

    # File-system-friendly timestamp: replace ':' which Windows hates
    # and which is ugly in shell expansions even on Linux.
    ts_for_path = timestamp.replace(":", "").replace("+0000", "Z")
    name = f"{ts_for_path}-{_safe_segment(kpi_name)}.md"
    path = fn_dir / name

    final = raw_response.get("final_verdict", "")
    status = raw_response.get("status", "")
    question = raw_response.get("question", "")
    context = raw_response.get("context", "")

    body_parts: List[str] = []
    body_parts.append(f"# Self-repair deliberation — {function} :: {kpi_name}")
    body_parts.append("")
    body_parts.append(f"- timestamp: {timestamp}")
    body_parts.append(f"- status: {status}")
    body_parts.append(f"- final_verdict: {final}")
    if raw_response.get("saved_to"):
        body_parts.append(f"- engine_transcript: {raw_response['saved_to']}")
    body_parts.append("")
    body_parts.append("## Question")
    body_parts.append("")
    body_parts.append("```")
    body_parts.append(str(question)[:8000])
    body_parts.append("```")
    body_parts.append("")
    body_parts.append("## Context (diagnostic bundle)")
    body_parts.append("")
    body_parts.append("```json")
    body_parts.append(str(context)[:16000])
    body_parts.append("```")
    body_parts.append("")
    body_parts.append("## Panel response (raw)")
    body_parts.append("")
    body_parts.append("```json")
    body_parts.append(json.dumps(raw_response, indent=2, default=str)[:32000])
    body_parts.append("```")

    path.write_text("\n".join(body_parts), encoding="utf-8")
    return str(path)


# ── fix-tier classification ──────────────────────────────────────────


# Order matters — most-specific patterns first. The classifier returns
# the first tier whose patterns match the panel's text. If nothing
# matches we fall back to `DEFAULT_FIX_TIER` ('code_change') so the
# founder is the gate by default.
_TIER_PATTERNS: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = [
    (
        "prompt_rewrite",
        (
            re.compile(r"\brewrite\s+(?:the\s+)?prompt\b", re.IGNORECASE),
            re.compile(r"\bprompt[-\s]rewrite\b", re.IGNORECASE),
            re.compile(r"\b(?:tune|tweak|update|adjust)\s+(?:the\s+)?prompt\b", re.IGNORECASE),
            re.compile(r"\bprompt\s+(?:tweak|update|tuning|adjustment)\b", re.IGNORECASE),
        ),
    ),
    (
        "kpi_adjust",
        (
            re.compile(r"\b(?:adjust|tune|relax|raise|lower)\s+(?:the\s+)?(?:kpi|threshold|floor|ceiling)\b", re.IGNORECASE),
            re.compile(r"\b(?:kpi|threshold)\s+(?:adjust(?:ment)?|tweak|tuning)\b", re.IGNORECASE),
            re.compile(r"\bkpi[-\s]adjust\b", re.IGNORECASE),
        ),
    ),
    (
        "disable_temp",
        (
            re.compile(r"\bdisable\s+(?:the\s+)?function\s+temporarily\b", re.IGNORECASE),
            re.compile(r"\btemporarily\s+disable\b", re.IGNORECASE),
            re.compile(r"\bdisable[-\s]temp\b", re.IGNORECASE),
            re.compile(r"\bpause\s+(?:the\s+)?function\b", re.IGNORECASE),
        ),
    ),
    (
        "scope_expansion",
        (
            re.compile(r"\bscope[-\s]expansion\b", re.IGNORECASE),
            re.compile(r"\bexpand\s+(?:the\s+)?scope\b", re.IGNORECASE),
            re.compile(r"\badd\s+(?:a\s+)?new\s+(?:venue|persona|platform)\b", re.IGNORECASE),
        ),
    ),
    (
        "spend_increase",
        (
            re.compile(r"\bspend[-\s]increase\b", re.IGNORECASE),
            re.compile(r"\bincrease\s+(?:the\s+)?(?:spend|budget|cost)\b", re.IGNORECASE),
            re.compile(r"\b(?:raise|bump)\s+(?:the\s+)?budget\b", re.IGNORECASE),
        ),
    ),
    (
        "code_change",
        (
            re.compile(r"\bcode[-\s]change\b", re.IGNORECASE),
            re.compile(r"\bchange\s+(?:the\s+)?(?:code|source|implementation)\b", re.IGNORECASE),
            re.compile(r"\bpatch\s+(?:the\s+)?(?:code|source|implementation)\b", re.IGNORECASE),
            re.compile(r"\bedit\s+(?:the\s+)?source(?:\s+code)?\b", re.IGNORECASE),
        ),
    ),
]


def _verdict_text(raw_response: Dict[str, Any]) -> str:
    """Extract the most-informative free-text from the deliberation
    response so the classifier + escalation scanner have something to
    work with.

    The gateway's `deliberate()` returns a transcript dict with at
    least `final_verdict`. Older / single-model paths add a `summary`
    or `synthesis` field; round dicts each carry `responses` keyed by
    model id. We concatenate everything we can find — the regex
    patterns are tolerant of noise.
    """
    parts: List[str] = []
    fv = raw_response.get("final_verdict")
    if isinstance(fv, str):
        parts.append(fv)
    for key in ("summary", "synthesis", "verdict_text", "fix_description"):
        val = raw_response.get(key)
        if isinstance(val, str):
            parts.append(val)

    rounds = raw_response.get("rounds")
    if isinstance(rounds, list):
        for r in rounds:
            if not isinstance(r, dict):
                continue
            responses = r.get("responses") or {}
            if isinstance(responses, dict):
                for v in responses.values():
                    if isinstance(v, str):
                        parts.append(v)
                    elif isinstance(v, dict):
                        # response objects sometimes wrap text in a
                        # 'response' or 'content' key
                        for k in ("response", "content", "text"):
                            inner = v.get(k)
                            if isinstance(inner, str):
                                parts.append(inner)
    return "\n".join(parts)


def _classify_fix_tier(panel_text: str) -> str:
    """Heuristic tier classification. Returns the first matching tier;
    if nothing matches, returns `DEFAULT_FIX_TIER` ('code_change').

    'code_change' is intentionally most-restrictive — when the panel
    is unparseable, the founder approves before anything happens.
    """
    if not panel_text:
        return DEFAULT_FIX_TIER
    for tier, patterns in _TIER_PATTERNS:
        for pat in patterns:
            if pat.search(panel_text):
                return tier
    return DEFAULT_FIX_TIER


def _build_fix_description(panel_text: str, *, max_chars: int = 600) -> str:
    """Pull a short description out of the panel text for the email.

    We prefer the first paragraph that mentions a fix verb — failing
    that we use the first `max_chars` characters of the final verdict
    so the founder can scan quickly.

    LED-1208/LED-1209: When the panel produces no readable text (or
    the engine errored), return a string carrying the
    `_FIX_PARSE_FAILED_MARKER`. Downstream code keys on the marker:

      - `_scan_escalation` returns an empty class (no false-positive
        ESCALATION email on a parse failure).
      - `render_verdict_email` substitutes a soft-warning body.
    """
    if not panel_text:
        return f"(panel produced no readable text {_FIX_PARSE_FAILED_MARKER})"
    # Try to find a sentence that starts with a verb suggestive of a
    # recommendation. Falls through to the first non-empty line.
    for line in panel_text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(
            low.startswith(verb)
            for verb in (
                "rewrite",
                "tweak",
                "adjust",
                "tune",
                "disable",
                "pause",
                "expand",
                "increase",
                "patch",
                "edit",
                "change",
                "raise",
                "lower",
                "relax",
            )
        ):
            return s[:max_chars]
    # Fallback: first `max_chars` chars of the trimmed text.
    trimmed = panel_text.strip().replace("\n\n", " ").replace("\n", " ")
    return trimmed[:max_chars]


# Marker the deliberate loop sets on `proposed_fix` when the panel
# response could not be reduced to a structured fix (no recommendation
# verb, empty text, engine error, etc). The escalation matcher reads
# this marker to default to "empty class" instead of grepping raw
# panel prose. Email rendering reads it to surface a soft warning so
# the founder knows the verdict needs manual review.
_FIX_PARSE_FAILED_MARKER = "__parse_failed__"


def _scan_escalation(
    panel_text: str = "",  # noqa: ARG001 — kept for backwards-compat callers
    fix: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Scan ONLY the structured `proposed_fix` for escalation hard-stop
    keywords. Returns the canonical hard-stop names that matched, in
    declaration order.

    LED-1208: The matcher previously scanned the raw `final_verdict` /
    panel response text, which produced false positives whenever a
    model said "we should NOT account_switch" or quoted a hard-stop
    keyword in negated context. Constrain the input to:

      - `proposed_fix.tier`
      - `proposed_fix.description` (already verb-anchored or 600-char
        excerpt — much narrower than the full panel response)
      - `proposed_fix.specifics` (dict values flattened)

    The first positional argument is retained so external callers and
    tests that pass `(panel_text, fix)` keep working, but the value is
    intentionally ignored — that's the entire point of LED-1208.

    Safe default: if the proposed_fix itself signalled a parse failure
    (`_FIX_PARSE_FAILED_MARKER` in description), return an empty class.
    The watcher promotes this to a soft warning in the email body so
    the founder reviews the verdict manually rather than being routed
    to the [self-repair-ESCALATION] track on a parse error.

    The watcher / apply gate uses this list to:
      - flip the email subject prefix to `[self-repair-ESCALATION]`
      - keep `requires_founder_approval = True` regardless of tier
      - refuse to auto-apply (next-layer concern)
    """
    if not fix:
        return []

    desc = fix.get("description")
    if isinstance(desc, str) and _FIX_PARSE_FAILED_MARKER in desc:
        # Parser failed — do NOT grep raw prose; safe default is empty.
        return []

    # Two haystacks:
    #   `structured`: tier + specifics — these are emitted by classifier
    #     / structured-output panel, so canonical snake_case hard-stop
    #     names appearing here are unambiguous.
    #   `prose`: description — partly free-text from the panel, can
    #     legitimately quote hard-stop names in negated context
    #     ("we do NOT recommend force_push_to_main"). We only run the
    #     natural-language regex patterns against this haystack and
    #     intentionally do NOT exact-match canonical names — that's
    #     the LED-1208 false-positive class.
    structured_parts: List[str] = []
    tier = fix.get("tier")
    if isinstance(tier, str):
        structured_parts.append(tier)
    specs = fix.get("specifics")
    if isinstance(specs, dict):
        try:
            structured_parts.append(json.dumps(specs, default=str))
        except (TypeError, ValueError):
            structured_parts.append(repr(specs))
    elif isinstance(specs, str):
        structured_parts.append(specs)

    structured_blob_lower = "\n".join(structured_parts).lower()

    prose_parts: List[str] = []
    if isinstance(desc, str):
        prose_parts.append(desc)
    prose_blob = "\n".join(prose_parts)

    matched: List[str] = []
    for hard_stop_name, patterns in _HARD_STOP_PATTERNS.items():
        # 1. Exact-match the canonical name in structured fields only.
        if hard_stop_name in structured_blob_lower:
            matched.append(hard_stop_name)
            continue
        # 2. Natural-language regex patterns against either haystack.
        # Description prose is allowed to contain negated mentions of
        # the natural-language form ("we should NOT force-push"), but
        # the regexes here are not negation-aware. The narrower
        # 200-char description cap (LED-1209) limits the false-positive
        # surface; the full guard against negated hard-stop quotes is
        # the structured-output panel format which puts the actual
        # action in `specifics` (caught by step 1 above).
        full_blob = prose_blob + "\n" + "\n".join(structured_parts)
        if not full_blob.strip():
            continue
        for pat in patterns:
            if pat.search(full_blob):
                matched.append(hard_stop_name)
                break
    return matched


# ── public entry-point ──────────────────────────────────────────────


def run_deliberation(
    breach: Breach,
    bundle: DiagnosticBundle,
    function_yaml: Dict[str, Any],
    deliberate_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    *,
    history_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    max_rounds: int = 3,
    mode: str = "debate",
    scope: str = "operational",
) -> DeliberationVerdict:
    """Run one panel deliberation against `bundle` for `breach`.

    Args:
      breach: the originating KPI breach.
      bundle: the diagnostic bundle the watcher already gathered.
      function_yaml: the loaded function KPI yaml. Currently consulted
          for completeness; the escalation hard-stop list lives in
          `_HARD_STOP_PATTERNS` at the module level so the loop's
          enforcement does not depend on per-function yaml fidelity.
      deliberate_fn: dependency-injected callable matching
          `delimit_deliberate`. Defaults to `_default_deliberate_fn`
          which dispatches to `ai.deliberation.deliberate`. Tests pass
          a stub.
      history_dir: override the transcript root (testing).
      now: override the timestamp (testing).
      max_rounds, mode, scope: forwarded to deliberate_fn.

    Returns:
      A `DeliberationVerdict` ready to be rendered into the email.
    """
    fn = deliberate_fn or _default_deliberate_fn
    anchor = now or datetime.now(tz=timezone.utc)
    timestamp = anchor.isoformat()

    question = _build_question(breach)
    context = _build_context(bundle)

    # Note: function_yaml is not currently read here, but accepting it
    # in the signature keeps the interface stable for the apply layer
    # which will need per-function tier-policy lookups.
    _ = function_yaml

    raw_panel_response: Dict[str, Any]
    try:
        raw_panel_response = fn(
            question=question,
            context=context,
            max_rounds=max_rounds,
            mode=mode,
            scope=scope,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("self_repair: deliberate_fn raised: %s", exc)
        raw_panel_response = {
            "error": f"deliberate_fn raised: {exc}",
            "final_verdict": "ERROR",
            "status": "error",
        }

    if not isinstance(raw_panel_response, dict):
        raw_panel_response = {
            "error": "deliberate_fn returned a non-dict",
            "raw_type": type(raw_panel_response).__name__,
            "final_verdict": "ERROR",
            "status": "error",
        }

    # Persist a transcript copy under self_repair_history.
    raw_for_transcript = {
        **raw_panel_response,
        "question": question,
        "context": context,
    }
    transcript_path = _save_transcript(
        function=breach.function,
        kpi_name=breach.kpi_name,
        timestamp=timestamp,
        raw_response=raw_for_transcript,
        history_dir=history_dir,
    )

    # Classify the fix tier from whatever text the panel emitted.
    panel_text = _verdict_text(raw_panel_response)
    fix_tier = _classify_fix_tier(panel_text)
    fix_description = _build_fix_description(panel_text)
    proposed_fix: Dict[str, Any] = {
        "tier": fix_tier,
        "description": fix_description,
        "specifics": {},
    }

    # Pre-flight escalation scan. CRITICAL: cannot be bypassed by panel
    # verdict; the loop refuses to recommend these regardless of what
    # the panel says.
    #
    # LED-1208: scans ONLY the structured proposed_fix (tier +
    # description + specifics). Raw panel text is intentionally NOT
    # passed in — that input produced false positives whenever the
    # panel quoted a hard-stop keyword in negated context. If the fix
    # itself failed to parse, the matcher returns an empty class and
    # the email body carries a soft "review manually" warning.
    escalation_class = _scan_escalation(fix=proposed_fix)

    # Status mapping. 'error' propagates from the engine. Otherwise
    # treat the response as 'unanimous' only if the engine explicitly
    # signaled it; everything else is 'split' so the founder is not
    # nudged into trusting a fix they shouldn't.
    if raw_panel_response.get("status") == "error" or "error" in raw_panel_response:
        status = "error"
    else:
        engine_verdict = str(raw_panel_response.get("final_verdict", "")).upper()
        if "UNANIMOUS" in engine_verdict and "DEADLOCK" not in engine_verdict:
            status = "unanimous"
        else:
            status = "split"

    # Escalation findings always demote the panel verdict to 'split' —
    # we will not auto-trust a fix that proposes a hard-stop class.
    if escalation_class and status != "error":
        status = "split"

    # Rounds count — the engine response either includes a 'rounds'
    # list (each entry is a round dict) or a top-level integer. Be
    # tolerant of both.
    rounds_field = raw_panel_response.get("rounds")
    if isinstance(rounds_field, list):
        rounds = len(rounds_field)
    elif isinstance(rounds_field, int):
        rounds = rounds_field
    else:
        rounds = 0

    final_verdict_summary = str(
        raw_panel_response.get("final_verdict") or "ERROR"
    )

    return DeliberationVerdict(
        breach=breach,
        bundle=bundle,
        timestamp=timestamp,
        status=status,
        rounds=rounds,
        final_verdict=final_verdict_summary,
        proposed_fix=proposed_fix,
        escalation_class=escalation_class,
        transcript_path=transcript_path,
        # v1: ALWAYS True. Panel verdict — no exceptions.
        requires_founder_approval=True,
        raw_panel_response=raw_panel_response,
    )


# ── email rendering ─────────────────────────────────────────────────


def render_verdict_email(verdict: DeliberationVerdict) -> Tuple[str, str]:
    """Render the verdict as a (subject, body) email tuple.

    LED-1209: this used to embed the panel's full chain-of-thought
    (~5KB of "I need to analyze this systematically..." preamble).
    The founder reviews these in their inbox, and a 5KB preamble
    before the actionable fix line was ~10 lines of noise.

    The email is now a short structured triage card:

      - Subject: `[self-repair-{mode}] {function} :: {kpi} :: {tier}`
        becomes `[self-repair-ESCALATION]` if any hard-stop tripped.
      - Body (≤500 chars before the transcript link):
          1. one-line breach summary (function, kpi, severity,
             actual vs threshold)
          2. fix tier
          3. fix description truncated to 200 chars
          4. fix specifics rendered as `key: value\\n`
          5. transcript link
          6. action id + approve/reject/info commands (existing)
      - NO raw panel response, NO chain-of-thought preamble.
      - If `proposed_fix.description` carries the parse-failed marker,
        the description is replaced with a soft warning telling the
        founder to review the transcript manually.
    """
    breach = verdict.breach
    fix = verdict.proposed_fix or {}
    tier = str(fix.get("tier") or DEFAULT_FIX_TIER)
    is_escalation = bool(verdict.escalation_class)

    if is_escalation:
        subject_prefix = "[self-repair-ESCALATION]"
    else:
        subject_prefix = "[self-repair-deliberate]"
    subject = (
        f"{subject_prefix} {breach.function} :: {breach.kpi_name} :: {tier}"
    )

    # Action ID for the approval-button block. Use a short, stable
    # identifier built from function + kpi + timestamp so the inbox
    # executor can correlate the reply back to this verdict.
    action_id = (
        f"sr-{_safe_segment(breach.function)}-"
        f"{_safe_segment(breach.kpi_name)}-"
        f"{_safe_segment(verdict.timestamp.replace(':', ''))}"
    )

    # ── description (with parse-failure soft warning) ──────────────
    raw_desc = fix.get("description")
    if not isinstance(raw_desc, str) or not raw_desc.strip():
        desc = (
            "Panel returned unparseable fix — review transcript manually."
        )
        parse_failed = True
    elif _FIX_PARSE_FAILED_MARKER in raw_desc:
        desc = (
            "Panel returned unparseable fix — review transcript manually."
        )
        parse_failed = True
    else:
        # Hard-cap description at 200 chars for the email card; the
        # transcript carries the full text.
        desc = raw_desc.strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        parse_failed = False

    lines: List[str] = []

    # 1. Breach summary — one line.
    lines.append(
        f"Breach: {breach.function} / {breach.kpi_name} "
        f"({breach.severity}, {breach.actual} vs {breach.threshold})"
    )

    # 2. Fix tier.
    lines.append(f"Fix tier: {tier}")

    # 3. Fix description.
    lines.append(f"Fix: {desc}")

    # 4. Fix specifics — flat `key: value` lines (NOT a JSON blob).
    specifics = fix.get("specifics") or {}
    if isinstance(specifics, dict) and specifics:
        lines.append("Specifics:")
        for k, v in specifics.items():
            try:
                vstr = (
                    json.dumps(v, default=str)
                    if not isinstance(v, str)
                    else v
                )
            except (TypeError, ValueError):
                vstr = repr(v)
            lines.append(f"  {k}: {vstr}")

    # Soft warning when the matcher could not extract a structured fix.
    # (LED-1208 safe default — keeps the breach surface visible without
    # routing parser errors to the [self-repair-ESCALATION] track.)
    if parse_failed and not is_escalation:
        lines.append(
            "Note: escalation pre-flight could not extract structured "
            "fix — review verdict manually."
        )

    # 5. Escalation warning — loud but compact.
    if is_escalation:
        lines.append("")
        lines.append("ESCALATION HARD-STOP DETECTED:")
        for cls in verdict.escalation_class:
            lines.append(f"  - {cls}")
        lines.append(
            "Self-repair CANNOT auto-apply; founder action required."
        )

    # 6. Transcript link — the natural break-point for the body cap.
    lines.append("")
    lines.append(
        f"View full panel transcript: {verdict.transcript_path or '(unsaved)'}"
    )

    # 7. Approval-action block. Mimics the social-draft approval pattern
    # so the inbox executor recognizes the same reply conventions.
    lines.append("")
    lines.append(f"Action ID: {action_id}")
    lines.append(f'  approve: reply "approve {action_id}"')
    lines.append(f'  reject:  reply "reject {action_id}"')
    lines.append(f'  info:    reply "info {action_id}"')
    lines.append("")
    lines.append(
        "v1 policy: ALL fixes require founder approval. No auto-apply, "
        "no exceptions."
    )

    return subject, "\n".join(lines)


# Helpful for tests / callers that want a deterministic dict view of
# the verdict (e.g. for json comparisons).
def verdict_to_dict(v: DeliberationVerdict) -> Dict[str, Any]:
    out = {
        "breach": asdict(v.breach),
        "function": v.breach.function,
        "kpi_name": v.breach.kpi_name,
        "timestamp": v.timestamp,
        "status": v.status,
        "rounds": v.rounds,
        "final_verdict": v.final_verdict,
        "proposed_fix": v.proposed_fix,
        "escalation_class": list(v.escalation_class),
        "transcript_path": v.transcript_path,
        "requires_founder_approval": v.requires_founder_approval,
    }
    return out
