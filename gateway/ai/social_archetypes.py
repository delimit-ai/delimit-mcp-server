"""Brand-voice autopost archetypes (founder-ratified 2026-06-19).

The @delimit_ai brand autoposts were reading like jargon feature-specs
("Four vendors, one independent merge gate. The verdict cites which model
produced which line."). Founder direction (RATIFIED, this is implementation):

  1. Punchier consumer-engagement voice — lead with the builder's real pain
     or a plain-words question; let the canon (merge gate / signed check /
     delimit.ai) land as the PAYOFF at the end, not the headline.
  2. Keep the FACTS on real updates — punchy is not factless. For genuine
     vendor features / product updates, keep the actual factual substance and
     just drop the jargon and tighten.
  3. VARIETY (hard requirement) — no single formula. Rotate distinct
     archetypes so the feed never reads like a template, with anti-repetition
     against recent posts.

Reference voice (founder launch tweet):
  "Your AI wrote 200 lines in 10 seconds. Did anyone read them?"

This module is the SINGLE source of truth for the 6 archetypes and the
anti-repetition selector. Both autopost paths consume it:

  * ``ai.social.generate_tailored_draft`` (brand twitter system prompt) — the
    shared voice engine, used by replies + the vendor riff path.
  * ``ai.vendor_news.drafter._build_riff_prompt`` — vendor-news riffs.

HARD CONSTRAINTS preserved (do NOT relax in archetype copy):
  * No first person on brand accounts (LED-791/LED-1246). Second-person
    ("you/your") and third-person only. The reference samples below are all
    second/third-person.
  * Vendor riffs MUST still pass capability_validator.validate_draft, which
    requires a canonical phrase (or matched allowed_claim) + a delimit.ai URL.
    The canon is DEMOTED below the hook, never removed.
  * No em dashes or en dashes in any generated tweet.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# Recent-post history sources used for anti-repetition. The vendor riff path
# writes vendor_news_history.jsonl (field: "text") and the general autopost
# path writes social_log.jsonl. We read whatever exists, newest entries last.
VENDOR_HISTORY_PATH = Path.home() / ".delimit" / "vendor_news_history.jsonl"
SOCIAL_LOG_PATH = Path.home() / ".delimit" / "social_log.jsonl"

# How many recent posts to weigh for anti-repetition.
_RECENT_WINDOW = 6


# ── the 6 ratified archetypes ─────────────────────────────────────────
#
# Each archetype carries:
#   key          stable id used in history + tests
#   label        human label
#   directive    the per-archetype instruction injected into the LLM prompt
#   reference    a ratified sample tweet (the voice target; verbatim from the
#                founder-approved seed set). These doubles as fallbacks if the
#                LLM path is unavailable, and as the grounding examples in the
#                prompt.
#
# All references are SECOND/THIRD-person (no "I"/"we"/"my"), canon-as-payoff,
# delimit.ai at the end, no em/en dashes. They are the ground-truth voice.

ARCHETYPES: List[Dict[str, str]] = [
    {
        "key": "pain_hook",
        "label": "pain-hook",
        "directive": (
            "PAIN-HOOK. Open with the builder's real, specific pain in plain "
            "words (no jargon). One short follow-up line that twists the knife "
            "or names the false comfort. Land the canon (a merge gate / a "
            "signed check / delimit.ai) as the PAYOFF in the final clause. "
            "Do NOT lead with the product."
        ),
        "reference": (
            "Shipping code you didn't read is the new normal. Hoping it's fine "
            "is not a plan. A 60-second merge gate that catches what your AI "
            "didn't mention: delimit.ai"
        ),
    },
    {
        "key": "question_hook",
        "label": "question-hook",
        "directive": (
            "QUESTION-HOOK. Open with ONE plain-words question the builder "
            "would actually ask themselves (not rhetorical fluff). Answer it "
            "in the same breath by naming the canon as the payoff. Exactly one "
            "question mark. Second person is fine ('you/your'). Do NOT ask the "
            "reader to do anything; the question is the hook, not a CTA."
        ),
        "reference": (
            "How do you actually know the code your AI just wrote is safe to "
            "merge? Not vibes. A merge gate that reads the diff and flags the "
            "breaking changes before merge: delimit.ai"
        ),
    },
    {
        "key": "scenario",
        "label": "scenario/story",
        "directive": (
            "SCENARIO / STORY. Tell a tiny concrete story in third person about "
            "what the AI did and what it quietly broke. Make it specific (an "
            "API response three files away, a 2am page). Land the canon as the "
            "fix at the end. No first person."
        ),
        "reference": (
            "The AI said 'done.' It also changed an API response three files "
            "away and didn't mention it. A merge gate for AI-written code "
            "catches that before it pages you at 2am: delimit.ai/reports"
        ),
    },
    {
        "key": "plain_update",
        "label": "plain factual update",
        "directive": (
            "PLAIN FACTUAL UPDATE (vendor news). State the real vendor feature "
            "in plain words, KEEPING the factual substance (that's the value). "
            "Drop the jargon. Then draw the honest consequence (more agents "
            "means more unread diffs) and land the canon as the payoff. Do not "
            "flatten the real update into a vibe; the fact is the hook."
        ),
        "reference": (
            "Cursor can now fan out agents from your phone and hand back PRs "
            "while you sleep. More agents means more diffs no human has read. "
            "A merge gate for AI-written code reads them first: delimit.ai"
        ),
    },
    {
        "key": "stat",
        "label": "stat/number",
        "directive": (
            "STAT / NUMBER. Open with a concrete number or a stat-shaped line "
            "(the reference '200 lines in 10 seconds' energy). Use it to expose "
            "how little gets read. Land the canon as the payoff. Only use "
            "numbers that are true or self-evidently illustrative; never invent "
            "metrics about Delimit's usage or revenue."
        ),
        "reference": (
            "Your AI wrote 200 lines in 10 seconds, and nobody read them. "
            "Claude, Codex, Gemini, and Grok all ship AI-written code nobody "
            "checks line-by-line. One merge gate that does: delimit.ai"
        ),
    },
    {
        "key": "contrarian",
        "label": "contrarian/myth",
        "directive": (
            "CONTRARIAN / MYTH-BUST. Open by quoting or naming a comfortable "
            "myth ('the AI tested it', 'it passed CI') and puncture it in plain "
            "words. Land the canon as the honest alternative payoff. No first "
            "person, no hedging."
        ),
        "reference": (
            "'The AI tested it' is not the same as 'someone checked it.' A "
            "signed, replayable attestation proves what actually ran before "
            "you merge: delimit.ai"
        ),
    },
]

_ARCHETYPE_BY_KEY: Dict[str, Dict[str, str]] = {a["key"]: a for a in ARCHETYPES}
ARCHETYPE_KEYS: List[str] = [a["key"] for a in ARCHETYPES]


# ── recent-post history (anti-repetition feedstock) ───────────────────


def _read_jsonl_texts(path: Path, limit: int) -> List[str]:
    """Return up to ``limit`` most-recent text strings from a JSONL log.

    Reads both the riff history shape ({"text": ...}) and the social_log
    shape ({"text": ...} / {"content": ...}). Returns newest-last (file
    order) then we take the tail. Graceful on any error.
    """
    if not path.exists():
        return []
    texts: List[str] = []
    archetypes: List[str] = []  # noqa: F841 (kept for shape clarity)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                txt = entry.get("text") or entry.get("content") or ""
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())
    except OSError:
        return []
    return texts[-limit:] if limit else texts


def recent_posts(
    window: int = _RECENT_WINDOW,
    *,
    vendor_history_path: Optional[Path] = None,
    social_log_path: Optional[Path] = None,
) -> List[str]:
    """Collect the most-recent brand posts across both history logs.

    Newest entries are at the end of each file; we merge the tails and keep
    the last ``window`` overall (best-effort ordering — the two logs are not
    globally timestamp-merged, but both contribute their freshest entries,
    which is what anti-repetition needs)."""
    vh = vendor_history_path or VENDOR_HISTORY_PATH
    sl = social_log_path or SOCIAL_LOG_PATH
    merged = _read_jsonl_texts(vh, window) + _read_jsonl_texts(sl, window)
    return merged[-window:] if window else merged


def recent_archetype_keys(
    window: int = _RECENT_WINDOW,
    *,
    vendor_history_path: Optional[Path] = None,
    social_log_path: Optional[Path] = None,
) -> List[str]:
    """Read recorded archetype keys from the riff history (if present).

    The drafter records ``archetype`` on each history row going forward. Older
    rows without the field are ignored. Returns newest-last."""
    vh = vendor_history_path or VENDOR_HISTORY_PATH
    keys: List[str] = []
    if not vh.exists():
        return keys
    try:
        with open(vh, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    k = entry.get("archetype")
                    if isinstance(k, str) and k in _ARCHETYPE_BY_KEY:
                        keys.append(k)
    except OSError:
        return keys
    return keys[-window:] if window else keys


# ── opener / CTA anti-repetition ──────────────────────────────────────


def _opener_signature(text: str) -> str:
    """Normalize the first few words of a post for opener-collision checks."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return " ".join(words[:4])


def _cta_signature(text: str) -> str:
    """Normalize the trailing canon/CTA clause for collision checks.

    We key on the last delimit.ai URL plus the handful of words before it,
    which is where the canon payoff lives."""
    low = (text or "").lower()
    m = list(re.finditer(r"delimit\.ai\S*", low))
    if not m:
        words = re.findall(r"[a-z0-9']+", low)
        return " ".join(words[-4:])
    end = m[-1].end()
    tail = low[:end]
    words = re.findall(r"[a-z0-9']+", tail)
    return " ".join(words[-5:])


# ── archetype selection with anti-repetition ──────────────────────────


def select_archetype(
    *,
    recent_keys: Optional[Sequence[str]] = None,
    avoid: Optional[Sequence[str]] = None,
    rng: Optional[random.Random] = None,
    window: int = _RECENT_WINDOW,
) -> Dict[str, str]:
    """Pick the next archetype, avoiding the recently-used ones.

    Strategy:
      * Never reuse the archetype used in the immediately-preceding post.
      * Prefer an archetype not seen in the last ``window`` posts at all.
      * If every archetype has been used recently (small rotation), fall back
        to the least-recently-used among them.

    Args:
        recent_keys: Explicit recent archetype-key list (newest last). When
            None, read from the riff history log.
        avoid: Extra keys to exclude this pick (e.g. caller-provided).
        rng: Optional Random for deterministic tests.
        window: Recency window.

    Returns:
        The chosen archetype dict (a member of ARCHETYPES).
    """
    r = rng or random
    if recent_keys is None:
        recent_keys = recent_archetype_keys(window=window)
    recent = [k for k in (recent_keys or []) if k in _ARCHETYPE_BY_KEY]
    avoid_set = {k for k in (avoid or []) if k in _ARCHETYPE_BY_KEY}

    last_used = recent[-1] if recent else None
    if last_used:
        avoid_set.add(last_used)

    recent_set = set(recent)

    # 1) archetypes never seen in the window and not explicitly avoided
    fresh = [k for k in ARCHETYPE_KEYS if k not in recent_set and k not in avoid_set]
    if fresh:
        return _ARCHETYPE_BY_KEY[r.choice(fresh)]

    # 2) anything not in avoid_set (i.e. allow window repeats but never the
    #    immediately-preceding archetype / explicit avoids)
    allowed = [k for k in ARCHETYPE_KEYS if k not in avoid_set]
    if allowed:
        # least-recently-used: order by last index in `recent` (smaller = older)
        def _recency(k: str) -> int:
            # higher index = more recent; missing = oldest possible
            idxs = [i for i, rk in enumerate(recent) if rk == k]
            return idxs[-1] if idxs else -1
        allowed.sort(key=_recency)
        return _ARCHETYPE_BY_KEY[allowed[0]]

    # 3) degenerate fallback (all avoided) — pick any
    return _ARCHETYPE_BY_KEY[r.choice(ARCHETYPE_KEYS)]


def get_archetype(key: str) -> Optional[Dict[str, str]]:
    return _ARCHETYPE_BY_KEY.get(key)


def archetype_reference_samples() -> List[str]:
    """Return the ratified reference tweet for every archetype (ordered)."""
    return [a["reference"] for a in ARCHETYPES]


# ── shared prompt fragments ───────────────────────────────────────────


def build_archetype_prompt_block(
    archetype: Dict[str, str],
    *,
    recent_posts_for_variety: Optional[Sequence[str]] = None,
) -> str:
    """Build the archetype-specific instruction block injected into the LLM
    prompt for BOTH autopost paths.

    The block carries:
      * the punchy-voice doctrine (hook first, canon as payoff),
      * the chosen archetype's directive + its ratified reference sample,
      * the hard rails (no first person, no em/en dashes, canon demoted but
        present), and
      * an anti-repetition note listing recent openers/CTAs to avoid.
    """
    lines: List[str] = [
        "BRAND AUTOPOST VOICE (founder-ratified). Match the energy of this "
        "reference: \"Your AI wrote 200 lines in 10 seconds. Did anyone read "
        "them?\"",
        "",
        "VOICE DOCTRINE:",
        "- Lead with the builder's real pain or a plain-words question. Plain "
        "language, not jargon.",
        "- Let the canon land as the PAYOFF at the END, never open with the "
        "product or a feature-spec.",
        "- CANON PAYOFF (REQUIRED, exact wording): the closing clause MUST "
        "contain at least one of these exact phrases verbatim, then the "
        "delimit.ai URL: 'merge gate', 'signed, replayable attestation', "
        "'AI-written code', or 'AI-assisted merge'. (You may add plain-words "
        "color like 'a 60-second merge gate', but the exact phrase has to be "
        "present so the canon is unambiguous.)",
        "- Punchy is NOT factless. For a real vendor feature, keep the actual "
        "factual substance and just drop the jargon.",
        "- No first person ever (no 'I', 'we', 'my', 'for me'). Second person "
        "('you/your') and third person only.",
        "- No em dashes or en dashes. Use commas, periods, or hyphens.",
        "- Max 3 sentences, under 50 words, under 280 characters.",
        "",
        f"ARCHETYPE FOR THIS POST: {archetype['label']}",
        f"- {archetype['directive']}",
        "",
        "RATIFIED REFERENCE FOR THIS ARCHETYPE (match its shape, do not copy "
        f"verbatim):\n  {archetype['reference']}",
    ]

    avoid_openers: List[str] = []
    avoid_ctas: List[str] = []
    for p in (recent_posts_for_variety or []):
        op = _opener_signature(p)
        if op:
            avoid_openers.append(op)
        cta = _cta_signature(p)
        if cta:
            avoid_ctas.append(cta)
    if avoid_openers or avoid_ctas:
        lines.append("")
        lines.append(
            "ANTI-REPETITION (the last few posts already used these; do NOT "
            "reuse the same opening words or the same closing canon phrasing):"
        )
        for op in avoid_openers[-_RECENT_WINDOW:]:
            lines.append(f"  recent opener: {op}")
        for cta in avoid_ctas[-_RECENT_WINDOW:]:
            lines.append(f"  recent closer: {cta}")

    return "\n".join(lines)


# ── post MODE: pitch vs value (2026-06-19, founder direction) ─────────
#
# The spam signal is not the post COUNT, it is "product pitch + link every
# time." Scheduled originals now alternate two modes ~50/50, anti-repeated
# (never the same mode twice in a row):
#
#   PITCH  — the current archetype voice: hook + canon + delimit.ai URL.
#            capability_validator (exact canon phrase + delimit.ai URL) is
#            ENFORCED, unchanged.
#   VALUE  — a twitter-adapted version of the existing reddit value-first
#            voice (proud builder, genuinely helpful, never salesy) using the
#            CURIOSITY-GAP / SPECIFICITY / PAIN-FIRST / SOLVED-IT tactics.
#            A pure-value take or observation about AI code review / shipping
#            AI-written code / the space. NO delimit.ai link required, NO
#            forced canon phrase, NO Delimit mention required — a sharp take
#            from @delimit_ai stands on its own (curiosity gap is the point).
#            capability_validator canon+URL is EXEMPT; the OTHER rails (no
#            first person, no em/en dashes, length, fit_floor) still apply.

MODE_PITCH = "pitch"
MODE_VALUE = "value"
MODES = [MODE_PITCH, MODE_VALUE]

# Ratio of PITCH posts among scheduled originals (~50/50).
PITCH_RATIO = 0.5

# Ratified VALUE-mode reference takes. Third-person brand voice (NOT
# first-person "I built"); each carries a fit_floor-recognized on-topic anchor
# (merge / review / ship / breaking change / AI agents / diff-on-a-PR) so a
# genuine on-topic take passes the selectivity floor without weakening it.
VALUE_REFERENCES: List[str] = [
    "When an AI agent opens a PR, most review still means a human scrolling a "
    "600-line diff at 5pm, hoping the part nobody read is fine before merge.",
    "The fastest way to ship a breaking change is to let an AI agent rename a "
    "response field and watch the PR go green.",
    "Reviewing an AI agent's PR is a different job than reviewing a human's. "
    "The breaking change hides in the boring parts everyone skims before merge.",
    "Four AI agents open four PRs in an afternoon. The bottleneck stopped being "
    "writing code and became reviewing the diffs nobody has time to read.",
    "A deterministic diff catches a renamed OpenAPI field every time. An AI "
    "agent's summary of the same PR catches it when it feels like it.",
    "The scary breaking change from an AI agent is not the obvious kind. It is "
    "the kind that ships green, passes review by vibes, and breaks prod.",
]


def value_reference_samples() -> List[str]:
    return list(VALUE_REFERENCES)


def recent_modes(
    window: int = _RECENT_WINDOW,
    *,
    vendor_history_path: Optional[Path] = None,
) -> List[str]:
    """Read recorded post modes from the shared history (newest-last).

    Rows written by the scheduled poster carry a ``mode`` field. Vendor riffs
    and ship tweets are inherently pitch-shaped (canon+URL) and are treated as
    PITCH when they carry no explicit mode, so the mix balances across ALL
    sources, not just scheduled originals."""
    vh = vendor_history_path or VENDOR_HISTORY_PATH
    modes: List[str] = []
    if not vh.exists():
        return modes
    try:
        with open(vh, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                m = entry.get("mode")
                if m in MODES:
                    modes.append(m)
                elif entry.get("source") in ("vendor_news_riff", "ship_event"):
                    # canon+URL sources count as pitch for mix balancing
                    modes.append(MODE_PITCH)
    except OSError:
        return modes
    return modes[-window:] if window else modes


def select_mode(
    *,
    recent_modes_list: Optional[Sequence[str]] = None,
    rng: Optional[random.Random] = None,
    window: int = _RECENT_WINDOW,
    pitch_ratio: float = PITCH_RATIO,
) -> str:
    """Pick the next post mode, anti-repeated and ~``pitch_ratio`` balanced.

    Rules:
      * NEVER the same mode twice in a row.
      * Otherwise steer toward the target ratio: if the recent window already
        skews pitch, pick value, and vice-versa. Ties break by ratio-weighted
        random so the long-run mix is ~50/50.
    """
    r = rng or random
    if recent_modes_list is None:
        recent_modes_list = recent_modes(window=window)
    recent = [m for m in (recent_modes_list or []) if m in MODES]

    last = recent[-1] if recent else None
    if last:  # hard anti-repeat: flip away from the immediately-preceding mode
        return MODE_VALUE if last == MODE_PITCH else MODE_PITCH

    # no history: ratio-weighted random
    return MODE_PITCH if r.random() < pitch_ratio else MODE_VALUE


def build_value_prompt_block(
    *,
    recent_posts_for_variety: Optional[Sequence[str]] = None,
) -> str:
    """Build the VALUE-mode instruction block for generate_tailored_draft.

    Twitter-adapted from the reddit value-first tactics (CURIOSITY GAP,
    SPECIFICITY AS PROOF, PAIN FIRST, SOLVED-IT ENERGY) but in THIRD-PERSON
    brand voice (no first-person 'I built'; phrase as observation). No URL, no
    canon phrase, no Delimit mention required."""
    lines: List[str] = [
        "BRAND VALUE-FIRST POST (founder direction 2026-06-19). This is a "
        "PURE-VALUE take, NOT a product pitch. The point is a sharp, useful "
        "observation that stands on its own.",
        "",
        "VALUE VOICE (twitter-adapted from the reddit value-first tactics):",
        "- PAIN FIRST: open with the real pain a builder feels shipping or "
        "reviewing AI-written code. Plain language.",
        "- CURIOSITY GAP: describe the problem or the fix WITHOUT naming a "
        "product. Do NOT mention Delimit, do NOT add a link, do NOT pitch.",
        "- SPECIFICITY AS PROOF: use a concrete detail or number (a 600-line "
        "diff, a renamed response field, four agents, 5pm) so it reads true.",
        "- SOLVED-IT / AUTHORITY: sound like someone who has already seen this "
        "failure class, not someone selling a fix.",
        "",
        "HARD RULES (still enforced):",
        "- NO first person ever (no 'I', 'we', 'my', 'for me'). Phrase as a "
        "third-person observation about the space, never 'I built' / 'we ship'.",
        "- NO delimit.ai link and NO canon phrase. A bare take is correct here.",
        "- NO em dashes or en dashes. Max 3 sentences, under 50 words, under "
        "280 characters.",
        "- ON-TOPIC ANCHOR (REQUIRED): the take MUST contain at least one of "
        "these phrases verbatim so the relevance gate recognizes it: 'AI agent' "
        "(or 'AI agents'), 'AI-written code', 'AI-generated code', 'breaking "
        "change', 'merge gate', 'vibe coding', 'MCP tool'. Weave it in naturally "
        "(say 'an AI agent's PR', not 'an AI's PR'). A take with none of these "
        "is rejected, so always name the AI-code subject explicitly.",
        "",
        "RATIFIED VALUE REFERENCES (match the shape + the no-link energy, do "
        "NOT copy verbatim):",
    ]
    for ref in VALUE_REFERENCES[:4]:
        lines.append(f"  {ref}")

    avoid_openers: List[str] = []
    for p in (recent_posts_for_variety or []):
        op = _opener_signature(p)
        if op:
            avoid_openers.append(op)
    if avoid_openers:
        lines.append("")
        lines.append(
            "ANTI-REPETITION (recent openers already used; do NOT reuse the "
            "same opening words):"
        )
        for op in avoid_openers[-_RECENT_WINDOW:]:
            lines.append(f"  recent opener: {op}")

    return "\n".join(lines)


# ── shared history writer (cross-source anti-repetition) ──────────────
#
# All three brand-autopost sources — vendor-news riffs, scheduled generated
# originals, and ship-event tweets — record their post to the SAME history
# log that ``select_archetype`` / ``recent_posts`` read, so they cross-avoid
# repetition. The vendor drafter already writes rows of this shape with an
# ``archetype`` field; this helper is the canonical writer for the two new
# sources (and is safe for the drafter to adopt later).


def opener_signature(text: str) -> str:
    """Public alias for the opener-collision signature."""
    return _opener_signature(text)


def cta_signature(text: str) -> str:
    """Public alias for the closing-canon collision signature."""
    return _cta_signature(text)


def record_history(
    text: str,
    *,
    archetype_key: Optional[str] = None,
    source: str = "",
    extra: Optional[Dict[str, Any]] = None,
    history_path: Optional[Path] = None,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a post to the shared history log read by the selector.

    Writes a row carrying at least ``text`` (so ``recent_posts`` sees it) and
    ``archetype`` (so ``recent_archetype_keys`` rotates away from it). Also
    stamps the opener/CTA signatures so downstream variety tooling does not
    have to recompute them. Best-effort: never raises into the caller.

    Args:
        text: the posted tweet text.
        archetype_key: the chosen archetype key (must be a known key to be
            recorded under ``archetype``; unknown keys are dropped).
        source: provenance tag ("scheduled_original", "ship_event",
            "vendor_news_riff", ...). Recorded for audit.
        extra: optional extra fields merged into the row (never clobbers the
            computed fields).
        history_path: override (test hook). Defaults to VENDOR_HISTORY_PATH —
            the SAME log the selector reads.
        now_iso: override timestamp (test hook).

    Returns:
        The row dict that was written (or attempted).
    """
    import datetime as _dt

    ts = now_iso or _dt.datetime.now(_dt.timezone.utc).isoformat()
    row: Dict[str, Any] = {
        "ts": ts,
        "text": text,
        "source": source or "unknown",
        "opener_sig": _opener_signature(text),
        "cta_sig": _cta_signature(text),
    }
    if archetype_key and archetype_key in _ARCHETYPE_BY_KEY:
        row["archetype"] = archetype_key
    if extra:
        for k, v in extra.items():
            row.setdefault(k, v)

    path = history_path or VENDOR_HISTORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:  # pragma: no cover — best-effort
        pass
    return row


__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_KEYS",
    "VENDOR_HISTORY_PATH",
    "SOCIAL_LOG_PATH",
    "recent_posts",
    "recent_archetype_keys",
    "record_history",
    "opener_signature",
    "cta_signature",
    "select_archetype",
    "get_archetype",
    "archetype_reference_samples",
    "build_archetype_prompt_block",
    "MODE_PITCH",
    "MODE_VALUE",
    "MODES",
    "PITCH_RATIO",
    "VALUE_REFERENCES",
    "value_reference_samples",
    "recent_modes",
    "select_mode",
    "build_value_prompt_block",
]
