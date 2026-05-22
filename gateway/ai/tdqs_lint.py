"""TDQS (Tool Definition Quality Score) linter for MCP tool docstrings.

Implements LED-2108. Glama's TDQS evaluates each MCP tool's docstring
across 6 dimensions on a 1-5 scale, then aggregates to a letter grade
A/B/C/D. This module parses a target file (default: ai/server.py),
extracts every @mcp.tool() decorated function, and scores its docstring
heuristically — no LLM calls, fully deterministic.

The 6 dimensions (per glama.ai/blog/2026-04-03-tool-definition-quality-score-tdqs):

1. side_effects        — does the description disclose what gets written /
                         called / notified / chained / destroyed; auth /
                         rate-limit notes when relevant.
2. conciseness         — appropriately sized, front-loaded with purpose,
                         free of redundancy.
3. coverage            — enough for an agent to succeed first try: error
                         handling, prerequisites, return shape.
4. parameter_semantics — each parameter has constraint/intent beyond the
                         schema's bare type.
5. disambiguation      — names a sibling tool or otherwise differentiates
                         this tool from its neighbors.
6. when_to_use         — explicit "Use when ..." / "Don't use when ...";
                         alternatives named.

Each dimension score is in [1, 5]. The aggregate grade maps from the mean:

    A: mean >= 4.5
    B: 3.5 <= mean < 4.5
    C: 2.5 <= mean < 3.5
    D: mean < 2.5

This module is import-safe and has no side effects on import. Use it via
the public functions :func:`lint_file` and :func:`score_tool`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Grade mapping ──────────────────────────────────────────────────────

# Inferred from Glama's letter-grade badge progression. Refine if Glama
# publishes the explicit thresholds. Boundary semantics: mean >= 4.5 is A,
# mean strictly < 2.5 is D, with B/C in between.
GRADE_THRESHOLDS = (
    ("A", 4.5),
    ("B", 3.5),
    ("C", 2.5),
    ("D", 0.0),
)


def grade_for_mean(mean: float) -> str:
    """Map a mean score in [1, 5] to a letter grade A/B/C/D."""
    for letter, floor in GRADE_THRESHOLDS:
        if mean >= floor:
            return letter
    return "D"


# ─── Tool extraction ───────────────────────────────────────────────────

def _is_mcp_tool_decorator(decorator: ast.expr) -> bool:
    """True if a decorator AST node is `@mcp.tool(...)` or `@mcp.tool`."""
    target = decorator
    if isinstance(decorator, ast.Call):
        target = decorator.func
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "mcp"
            and target.attr == "tool"
        )
    return False


def _function_param_names(node: ast.FunctionDef) -> List[str]:
    """Return positional + keyword-only param names, excluding self/cls."""
    args = node.args
    names: List[str] = []
    for arg in args.posonlyargs + args.args + args.kwonlyargs:
        if arg.arg in ("self", "cls"):
            continue
        names.append(arg.arg)
    return names


def _function_body_text(source: str, node: ast.FunctionDef) -> str:
    """Return the source text of the function body (best effort)."""
    try:
        return ast.get_source_segment(source, node) or ""
    except Exception:
        return ""


def extract_tools(source: str) -> List[Dict[str, Any]]:
    """Parse `source` and return a record per @mcp.tool()-decorated function.

    Each record has: name, docstring, params, body_text, lineno, has_decorator.
    Functions without docstrings are still returned (with docstring="") so
    they can be flagged as zero-coverage by the scorer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    tools: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            continue
        docstring = ast.get_docstring(node) or ""
        tools.append(
            {
                "name": node.name,
                "docstring": docstring,
                "params": _function_param_names(node),
                "body_text": _function_body_text(source, node),
                "lineno": node.lineno,
                "has_decorator": True,
            }
        )
    return tools


# ─── Per-dimension scorers ─────────────────────────────────────────────
#
# Each scorer returns (score, hint) where score is in [1, 5] and hint is a
# short remediation note to display when score < 4. Scorers must be
# deterministic and side-effect-free.

# Vocabulary lookups used across scorers. Pre-compiled where useful.
_SIDE_EFFECT_KEYWORDS = (
    "writes", "write", "wrote",
    "calls", "call ", "calling",
    "notifies", "notify",
    "chains", "chain", "auto-chain", "auto-chains",
    "modifies", "modify",
    "creates", "create",
    "deletes", "delete", "destroys",
    "records", "record ",
    "fetches", "fetch", "downloads",
    "posts ", "post to", "publishes",
    "raises", "returns",
    "no side effects", "side-effect-free", "pure",
    "auth", "license", "rate limit", "rate-limit",
    "side effects", "side effect",
)

_BOILERPLATE_PHRASES = (
    "this function",
    "this tool will",
    "as a function",
    "you can use this",
)

_SIBLING_PATTERNS = (
    re.compile(r"\bunlike\s+\w+", re.IGNORECASE),
    re.compile(r"\bdiffer(s|ent)\s+from\b", re.IGNORECASE),
    re.compile(r"\bvs\.?\s+`?delimit_\w+", re.IGNORECASE),
    re.compile(r"\bsibling\b", re.IGNORECASE),
    re.compile(r"\bcomplements?\b", re.IGNORECASE),
    re.compile(r"\bcompare(?:d|s)?\s+(?:to|with)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+to\s+be\s+confused\s+with\b", re.IGNORECASE),
    re.compile(r"`?delimit_\w+`?\s+(?:is|does|handles|covers)", re.IGNORECASE),
    re.compile(r"\buse\s+`?delimit_\w+", re.IGNORECASE),
    re.compile(r"\bsee\s+also\b", re.IGNORECASE),
)

_WHEN_TO_USE_PATTERNS = (
    re.compile(r"\buse\s+(?:this|when)\b", re.IGNORECASE),
    re.compile(r"\bwhen\s+to\s+use\b", re.IGNORECASE),
    re.compile(r"\b(?:do\s*not|don'?t)\s+use\b", re.IGNORECASE),
    re.compile(r"\bnot\s+for\b", re.IGNORECASE),
    re.compile(r"\bprimary\s+(?:integration|use|case)\b", re.IGNORECASE),
    re.compile(r"\bfor\s+\w+\s*,\s*use\b", re.IGNORECASE),
    re.compile(r"\b(?:useful|helpful)\s+(?:for|when)\b", re.IGNORECASE),
)

_PARAM_HINT_PATTERNS = (
    re.compile(r"\b(default|defaults\s+to|default:)\b", re.IGNORECASE),
    re.compile(r"\b(must|should|required|optional)\b", re.IGNORECASE),
    re.compile(r"\b(e\.?g\.?|i\.?e\.?|example)\b", re.IGNORECASE),
    re.compile(r"\b(range|max|min|maximum|minimum|<=|>=|cap|capped)\b", re.IGNORECASE),
    re.compile(r"\b(true|false)\b", re.IGNORECASE),
    re.compile(r"\b(path|url|json|yaml|repo|spec)\b", re.IGNORECASE),
    re.compile(r":\s*\w"),  # colon followed by description text
)


def score_side_effects(doc: str, body: str) -> Tuple[int, str]:
    """Score 1-5 on side-effect disclosure.

    A docstring that names what it writes / calls / chains scores high;
    one that elides side effects when the body clearly performs them
    scores low.
    """
    doc_l = doc.lower()
    keyword_hits = sum(1 for kw in _SIDE_EFFECT_KEYWORDS if kw in doc_l)

    # Body-level evidence of side effects we expect to see disclosed
    body_l = body.lower()
    body_writes = any(
        s in body_l
        for s in (
            "_ledger_add", "_safe_call", "subprocess.", "requests.",
            "urlopen", "open(", ".write(", "json.dump", "yaml.dump",
            "_audit_event", "_record_evidence", "logger.warning",
            "notify_inbox", "send_notification", "supabase",
        )
    )
    body_pure = not body_writes and len(body) < 400

    if keyword_hits >= 4:
        score = 5
    elif keyword_hits >= 3:
        score = 4
    elif keyword_hits >= 2:
        score = 3
    elif keyword_hits >= 1:
        score = 2
    else:
        score = 1

    # Penalty: body has writes but doc says nothing about them.
    if body_writes and keyword_hits < 2:
        score = min(score, 2)
    # Bonus: pure helper with explicit "no side effects" / "returns" wording
    # earns at least a 4.
    if body_pure and ("returns" in doc_l or "no side effects" in doc_l):
        score = max(score, 4)

    hints = []
    if score < 4:
        hints.append(
            "name what is written/called/chained "
            "(e.g. 'writes to ledger', 'auto-chains delimit_evidence_collect')"
        )
    if body_writes and keyword_hits < 2:
        hints.append("body shows writes/calls but docstring does not disclose them")
    return score, "; ".join(hints)


def score_conciseness(doc: str) -> Tuple[int, str]:
    """Score 1-5 on conciseness and front-loaded purpose."""
    if not doc.strip():
        return 1, "no docstring"

    length = len(doc)
    first_sentence = doc.split(".")[0].strip()
    first_lower = first_sentence.lower()
    word_count = len(first_sentence.split())

    score = 5

    # Length window: 50-500 chars is healthy; punish either extreme.
    if length < 50:
        score = min(score, 2)
    elif length > 1500:
        score = min(score, 2)
    elif length > 800:
        score = min(score, 3)

    # Front-loaded purpose: first sentence should be an action+object.
    # Heuristic: first word is a verb (capitalized non-article) and the
    # sentence is between 4 and 25 words.
    if word_count < 3:
        score = min(score, 3)
    elif word_count > 30:
        score = min(score, 3)

    # Boilerplate phrases drag the score.
    for phrase in _BOILERPLATE_PHRASES:
        if phrase in first_lower:
            score = min(score, 3)
            break

    hints = []
    if score < 4:
        if length < 50:
            hints.append("docstring is too short (<50 chars)")
        elif length > 800:
            hints.append("docstring is very long (>800 chars), trim or restructure")
        if word_count < 3:
            hints.append("first sentence is too short to convey purpose")
        elif word_count > 30:
            hints.append("first sentence is too long; lead with verb+object")
        for phrase in _BOILERPLATE_PHRASES:
            if phrase in first_lower:
                hints.append(f"avoid boilerplate phrase '{phrase}'")
                break
    return score, "; ".join(hints)


def score_coverage(doc: str, params: List[str]) -> Tuple[int, str]:
    """Score 1-5 on whether the docstring lets an agent succeed first try."""
    if not doc.strip():
        return 1, "no docstring"

    doc_l = doc.lower()
    has_args = "args:" in doc_l or "arguments:" in doc_l or "parameters:" in doc_l
    has_returns = "returns:" in doc_l or "returns " in doc_l or "return value" in doc_l
    has_errors = (
        "raises:" in doc_l
        or "errors:" in doc_l
        or "error:" in doc_l
        or "fails" in doc_l
        or "exception" in doc_l
    )
    has_prereq = (
        "prerequisite" in doc_l
        or "requires" in doc_l
        or "before" in doc_l
        or "auth" in doc_l
    )

    score = 1
    if params and has_args:
        score += 2
    elif not params:
        # No params — Args section is optional, give partial credit.
        score += 1
    if has_returns:
        score += 1
    if has_errors or has_prereq:
        score += 1

    score = min(score, 5)

    hints = []
    if params and not has_args:
        hints.append("add Args: section documenting each parameter")
    if not has_returns:
        hints.append("describe the return shape (Returns: ...)")
    if not (has_errors or has_prereq):
        hints.append("note prerequisites or error conditions where they exist")
    return score, "; ".join(hints)


def score_parameter_semantics(doc: str, params: List[str]) -> Tuple[int, str]:
    """Score 1-5 on whether docstring clarifies param intent beyond schema."""
    if not params:
        # No params — neutral 4 (cannot fail this dimension by absence).
        return 4, ""

    if not doc.strip():
        return 1, "no docstring; cannot describe params"

    # Try to grab the Args block. We accept the Google style.
    args_match = re.search(
        r"(?:Args|Arguments|Parameters):\s*\n(.*?)(?:\n\s*\n|\Z|\n[A-Z][a-z]+:)",
        doc,
        re.DOTALL,
    )
    if not args_match:
        return 1, "no Args block found"

    args_block = args_match.group(1)

    documented = 0
    well_described = 0
    for p in params:
        # match `param:` at the start of a line (with optional indent).
        param_re = re.compile(rf"^\s*{re.escape(p)}\s*:\s*(.*)$", re.MULTILINE)
        m = param_re.search(args_block)
        if not m:
            continue
        documented += 1
        desc = m.group(1).strip()
        # Pull continuation lines that begin with deeper indent
        if len(desc) < 5:
            continue
        # Has at least one constraint/intent hint?
        if any(rx.search(desc) for rx in _PARAM_HINT_PATTERNS) and len(desc) > 12:
            well_described += 1

    if not documented:
        return 1, "no params documented in Args block"

    coverage = documented / len(params)
    quality = well_described / len(params)

    # Combined score: weighted average, capped at 5.
    raw = 1 + (coverage * 2) + (quality * 2)
    score = int(round(raw))
    score = max(1, min(5, score))

    hints = []
    if coverage < 1.0:
        hints.append(
            f"only {documented}/{len(params)} parameters documented in Args block"
        )
    if quality < 0.6:
        hints.append(
            "param descriptions lack constraints/defaults/examples beyond bare types"
        )
    return score, "; ".join(hints)


def score_disambiguation(doc: str, name: str) -> Tuple[int, str]:
    """Score 1-5 on whether docstring differentiates this tool from siblings."""
    if not doc.strip():
        return 1, "no docstring"

    # Self-mentions don't count.
    self_pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    doc_for_match = self_pattern.sub("", doc)

    matches = sum(1 for rx in _SIBLING_PATTERNS if rx.search(doc_for_match))
    differentiator_words = sum(
        1
        for w in (
            "unlike", "differs", "vs ", "vs.", "alternative", "instead of",
            "rather than", "prefer", "complement", "compared to",
        )
        if w in doc.lower()
    )

    if matches >= 2 or differentiator_words >= 2:
        score = 5
    elif matches >= 1 or differentiator_words >= 1:
        score = 4
    elif "delimit_" in doc_for_match.lower():
        score = 3
    elif len(doc) > 200:
        score = 2  # long but no sibling reference
    else:
        score = 2

    hints = []
    if score < 4:
        hints.append(
            "name a sibling tool and contrast (e.g. 'unlike delimit_diff, this also enforces policy')"
        )
    return score, "; ".join(hints)


def score_when_to_use(doc: str) -> Tuple[int, str]:
    """Score 1-5 on whether docstring offers usage / anti-usage guidance."""
    if not doc.strip():
        return 1, "no docstring"

    use_hits = sum(1 for rx in _WHEN_TO_USE_PATTERNS if rx.search(doc))
    has_when = bool(re.search(r"\bwhen\s+to\s+use\b", doc, re.IGNORECASE))
    has_when_not = bool(
        re.search(r"\bwhen\s+(?:not|NOT)\s+to\s+use\b", doc, re.IGNORECASE)
        or re.search(r"\b(?:do\s*not|don'?t)\s+use\b", doc, re.IGNORECASE)
    )

    if has_when and has_when_not:
        score = 5
    elif has_when or use_hits >= 2:
        score = 4
    elif use_hits >= 1:
        score = 3
    elif len(doc) > 200:
        score = 2
    else:
        score = 1

    hints = []
    if score < 4:
        hints.append("add explicit 'When to use:' / 'When NOT to use:' guidance")
    return score, "; ".join(hints)


# ─── Aggregation ───────────────────────────────────────────────────────

DIMENSIONS = (
    "side_effects",
    "conciseness",
    "coverage",
    "parameter_semantics",
    "disambiguation",
    "when_to_use",
)


def score_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Score a single tool record (as returned by extract_tools)."""
    doc = tool.get("docstring") or ""
    params = tool.get("params") or []
    body = tool.get("body_text") or ""
    name = tool.get("name") or ""

    s1, h1 = score_side_effects(doc, body)
    s2, h2 = score_conciseness(doc)
    s3, h3 = score_coverage(doc, params)
    s4, h4 = score_parameter_semantics(doc, params)
    s5, h5 = score_disambiguation(doc, name)
    s6, h6 = score_when_to_use(doc)

    scores = {
        "side_effects": s1,
        "conciseness": s2,
        "coverage": s3,
        "parameter_semantics": s4,
        "disambiguation": s5,
        "when_to_use": s6,
    }
    hints = {
        "side_effects": h1,
        "conciseness": h2,
        "coverage": h3,
        "parameter_semantics": h4,
        "disambiguation": h5,
        "when_to_use": h6,
    }
    mean = sum(scores.values()) / len(scores)
    grade = grade_for_mean(mean)

    defects = [
        {"dim": dim, "score": scores[dim], "hint": hints[dim]}
        for dim in DIMENSIONS
        if scores[dim] < 4
    ]

    return {
        "name": name,
        "lineno": tool.get("lineno"),
        "scores": scores,
        "mean_score": round(mean, 2),
        "grade": grade,
        "defects": defects,
    }


def aggregate(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll up per-tool scores into a corpus-level grade."""
    if not tool_results:
        return {"grade": "N/A", "mean_score": 0.0, "dim_means": {}, "tool_count": 0}

    dim_means: Dict[str, float] = {}
    for dim in DIMENSIONS:
        dim_means[dim] = round(
            sum(t["scores"][dim] for t in tool_results) / len(tool_results), 2
        )

    overall = round(sum(t["mean_score"] for t in tool_results) / len(tool_results), 2)
    return {
        "grade": grade_for_mean(overall),
        "mean_score": overall,
        "dim_means": dim_means,
        "tool_count": len(tool_results),
    }


def lint_file(target_file: str) -> Dict[str, Any]:
    """Lint a Python source file and return TDQS results.

    Args:
        target_file: Path to a Python file containing @mcp.tool()-decorated functions.

    Returns:
        {tools: [...], aggregate: {...}, target_file: ...}
    """
    path = Path(target_file)
    if not path.exists():
        return {
            "error": f"target_file not found: {target_file}",
            "tools": [],
            "aggregate": {"grade": "N/A", "mean_score": 0.0, "dim_means": {}, "tool_count": 0},
            "target_file": target_file,
        }

    source = path.read_text(encoding="utf-8")
    raw_tools = extract_tools(source)
    scored = [score_tool(t) for t in raw_tools]
    return {
        "tools": scored,
        "aggregate": aggregate(scored),
        "target_file": str(path),
    }


def render_human(result: Dict[str, Any]) -> str:
    """Render a lint_file result as a human-readable report."""
    if result.get("error"):
        return f"ERROR: {result['error']}"

    agg = result["aggregate"]
    lines = [
        f"TDQS lint report — {result['target_file']}",
        f"Tools scored: {agg['tool_count']}",
        f"Aggregate grade: {agg['grade']}  (mean={agg['mean_score']:.2f})",
        "Per-dimension means:",
    ]
    for dim, mean in agg.get("dim_means", {}).items():
        lines.append(f"  {dim:<22} {mean:.2f}")
    lines.append("")

    # Worst-first ordering helps remediation.
    worst = sorted(result["tools"], key=lambda t: t["mean_score"])
    lines.append("Tools with defects (worst first):")
    for t in worst:
        if not t["defects"]:
            continue
        lines.append(
            f"  [{t['grade']}] {t['name']} "
            f"(mean={t['mean_score']:.2f}, line {t['lineno']})"
        )
        for d in t["defects"]:
            hint = d["hint"] or "(no specific hint)"
            lines.append(f"      - {d['dim']}: {d['score']}/5 — {hint}")
    return "\n".join(lines)
