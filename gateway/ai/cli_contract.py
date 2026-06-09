"""LED-1415 — CLI subprocess contract.

The deliberation engine drives 4 model CLIs as subprocesses
(claude / codex / gemini / cursor) and treats their stdout as model
verdict text. Three classes of bug have surfaced in this pipeline:

  1. Banner contamination — the Delimit governance shim leaks ASCII
     art onto stdout instead of stderr (PR #154, fixed by LED-1428).
  2. Empty/silent responses — CLI exits 0 but stdout is empty
     (transient API issues, OOM, network blips). Caught by LED-1416's
     retry state machine.
  3. Schema drift — CLI changes its output shape between versions
     (e.g., adds an auto-correction line at the top). Caught
     reactively by failing deliberation panels.

This module holds the ONE contract that every CLI response must
satisfy + the ONE validator that enforces it. Both the per-CLI mock
tests (tests/test_cli_contract.py) AND the weekly real-CLI smoke
script (scripts/smoke_cli_contracts.py) call validate_cli_contract()
so the contract definition lives in exactly one place — extending
it doesn't require changing two places to remember.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# The 4 known CLIs the deliberation engine targets. cursor is included
# even though it's not yet installed in the dev environment — adding
# it to the contract surface now means the validator is ready when it
# lands; smoke skips when the binary isn't present.
KNOWN_CLI_NAMES = ("claude", "codex", "gemini", "cursor")


# Minimum scrubbed-response length we'll accept as "looks like a real
# model verdict" rather than "leftover garbage after banner strip."
# Calibrated against historical scrub-debug.jsonl entries: every real
# round-1/round-2 verdict from past deliberations was >= 60 chars;
# every banner-only contamination was < 30 chars. 30 is the cutoff
# the production scrubber already uses; keeping that here means the
# validator + the scrubber agree.
MIN_VERDICT_LEN = 30


# Patterns that signal "the response is contamination, not a verdict."
# Each gets the response REJECTED even if length and scrub passed.
_CONTAMINATION_MARKERS = (
    re.compile(r"^\[scrub:\s*contaminated\b", re.IGNORECASE),
    re.compile(r"^\[.+\bunavailable\b.+\bnot found in PATH\]", re.IGNORECASE),
    re.compile(r"^\[.+\bskipped under INTERNAL_PYTEST_GUARD", re.IGNORECASE),
    re.compile(r"^\[.+\btimed out after\b", re.IGNORECASE),
    re.compile(r"^\[.+\breturned empty response\]", re.IGNORECASE),
    re.compile(r"^\[.+\berror:.+\]\s*$", re.IGNORECASE),
)


# A response should contain at least ONE of these markers to be
# recognizable as a panel verdict. The deliberation engine prompts all
# models to end with `VERDICT: ...` so we expect to see it. Falling
# back: "AGREE" / "DISAGREE" / "REMEDIATE" / "AGREE WITH MODIFICATIONS"
# all appear in real responses even when the trailing VERDICT line is
# omitted by a chatty model.
# LED-1415: specific patterns for common provider-side blocks (rate limits, caps)
_PROVIDER_BLOCK_RE = re.compile(
    r"\b("
    r"weekly\s+limit|monthly\s+spend\s+limit|rate\s+limit|too\s+many\s+requests|"
    r"quota\s+exhausted|insufficient\s+balance|billing\s+account\s+not\s+active"
    r")\b",
    re.IGNORECASE,
)

_VERDICT_HINT_RE = re.compile(
    r"\b(VERDICT:|AGREE|DISAGREE|REMEDIATE|APPROVE|REJECT)\b",
    re.IGNORECASE,
)


@dataclass
class CliContractResult:
    """Outcome of validating one CLI's response.

    `ok` is True iff every contract clause passed. `failures` is the
    list of clauses that fired — the smoke script ntfys with this list
    so the operator can see exactly what shape the regression took.
    """
    cli: str
    raw_len: int
    scrubbed_len: int
    ok: bool
    failures: List[str] = field(default_factory=list)
    preview: str = ""  # First 200 chars of scrubbed text, for log readability


def validate_cli_contract(
    cli_name: str,
    raw_stdout: str,
    raw_stderr: str = "",
    expect_verdict_hint: bool = True,
) -> CliContractResult:
    """Apply the per-CLI contract to one subprocess response.

    Mirrors the EXACT production scrub path so the validator's view
    matches what ai/deliberation.py's _call_cli sees. Failures append
    a short reason string; an empty failures list means the response
    is contract-clean.

    Args:
        cli_name: which CLI produced this (claude/codex/gemini/cursor);
            used in the failure messages.
        raw_stdout: subprocess.stdout bytes decoded to str.
        raw_stderr: subprocess.stderr bytes decoded to str. The
            contract is permissive on stderr — banner output is
            ALLOWED there (intentional shim behavior); but completely
            empty stderr + completely empty stdout is suspicious.
        expect_verdict_hint: when True, fail the response if it
            doesn't contain at least one verdict marker. Mock tests
            and the smoke script set this; tests of low-content
            responses (e.g., a `--version` smoke) set False.

    Returns:
        CliContractResult with `ok`, `failures`, and a preview.
    """
    # Import lazily so this module can be imported in a context where
    # ai.deliberation isn't available (e.g., the smoke script when
    # gateway code path changes).
    failures: List[str] = []
    try:
        from ai.deliberation import _scrub_cli_output
        scrubbed = _scrub_cli_output(raw_stdout, source=cli_name).strip()
    except Exception as exc:
        return CliContractResult(
            cli=cli_name,
            raw_len=len(raw_stdout),
            scrubbed_len=0,
            ok=False,
            failures=[f"scrub_failed:{type(exc).__name__}:{str(exc)[:80]}"],
            preview="",
        )

    # 1. Contamination markers — if the scrubber returned one, fail.
    for pat in _CONTAMINATION_MARKERS:
        if pat.search(scrubbed):
            failures.append(f"contamination_marker:{pat.pattern[:40]}")
            break

    # 2. Minimum length. Below MIN_VERDICT_LEN is almost certainly
    # garbage even if scrub didn't tag it.
    if len(scrubbed) < MIN_VERDICT_LEN and "contamination_marker" not in " ".join(failures):
        failures.append(f"too_short:{len(scrubbed)}<{MIN_VERDICT_LEN}")

    # 3. Verdict hint — at least one of VERDICT:/AGREE/DISAGREE/REMEDIATE/
    # APPROVE/REJECT must appear. Skip when expect_verdict_hint=False.
    if _PROVIDER_BLOCK_RE.search(scrubbed):
        failures.append("provider_rate_limit_or_cap")
    
    if expect_verdict_hint and not _VERDICT_HINT_RE.search(scrubbed):
        failures.append("no_verdict_hint")

    # 4. Doesn't start with a known banner prefix (defense-in-depth on
    # top of scrub). If a brand-new banner shape lands tomorrow that
    # the scrubber doesn't know about, this should catch it.
    if scrubbed.startswith("["):
        # Bracketed prefix is almost always a tool-emitted status line
        # (e.g. "[Delimit]" / "[claude error: ...]") not a model verdict.
        if not any(scrubbed.lower().startswith(p) for p in (
            "[delimit", "[scrub:", "[claude", "[codex", "[gemini", "[cursor",
        )):
            # Unknown bracketed prefix — surface for inspection
            failures.append(f"unknown_bracketed_prefix:{scrubbed[:40]!r}")

    return CliContractResult(
        cli=cli_name,
        raw_len=len(raw_stdout),
        scrubbed_len=len(scrubbed),
        ok=not failures,
        failures=failures,
        preview=scrubbed[:200],
    )


def format_contract_report(results: List[CliContractResult]) -> str:
    """Human-readable summary of N validation results for ntfy / logs."""
    lines = []
    n_ok = sum(1 for r in results if r.ok)
    lines.append(f"CLI contract: {n_ok}/{len(results)} clean")
    for r in results:
        flag = "OK" if r.ok else "FAIL"
        lines.append(f"  [{flag}] {r.cli:8s} raw={r.raw_len}B scrubbed={r.scrubbed_len}B")
        if not r.ok:
            for f in r.failures:
                lines.append(f"           ↳ {f}")
            if r.preview:
                lines.append(f"           preview: {r.preview[:100]!r}")
    return "\n".join(lines)
