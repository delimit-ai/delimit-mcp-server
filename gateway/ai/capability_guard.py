"""Structured containment for tools whose backend is INTERNAL-excluded.

WHY THIS EXISTS
---------------
`bundle-classification.md` asserted:

    "Import-safety verified: no PUBLIC .py has a top-level import of an
     INTERNAL .py (lazy imports inside tool bodies are fine — those internal
     tools simply no-op on a public install)."

The first half was true and enforced. The second half was an assumption, and
it was wrong. A lazy `from ai.<internal> import ...` inside a tool body does
not no-op on a public install — it raises an unhandled ModuleNotFoundError and
the customer sees a raw Python traceback.

Measured on a clean install of delimit-cli 4.19.2: 15 registered tools raised
`ModuleNotFoundError`, including `delimit_scan` and `delimit_quickstart` —
the two the README points a new user at, and the ones the `premium_required`
payload names in its own `free_alternatives` list.

WHAT THIS IS AND IS NOT
-----------------------
This makes the classification doc's claim TRUE: an INTERNAL-backed tool now
genuinely no-ops, returning a structured `capability_unavailable` that says
plainly what is unavailable and why.

It is NOT a substitute for shipping a promised capability. `delimit_scan` and
`delimit_quickstart` are fixed by shipping the compiled `deliberation` module
they depend on, per that module's PROPRIETARY classification — not by being
quietly downgraded to "unavailable". Containment is for capabilities that are
genuinely internal-only; restoration is for capabilities we advertise.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Backends that bundle-classification.md marks INTERNAL. A tool that needs one
# of these is an internal operations capability, not shipped product.
INTERNAL_BACKENDS: Dict[str, str] = {
    "ai.loop_engine": "autonomous loop engine",
    "ai.reddit_scanner": "Reddit sensing/BD",
    "ai.github_scanner": "GitHub sensing/BD",
    "ai.social_target": "social/marketing automation",
    "ai.social_daemon": "social posting daemon",
    "ai.content_engine": "content scheduling engine",
    "ai.content_intel": "content intelligence",
    "ai.daily_digest": "autonomous loop daily digest",
    "ai.vendor_news": "vendor news sensing",
    "ai.siem_streaming": "SIEM streaming",
    "ai.inbox_daemon": "inbox daemon",
    "ai.daemon": "background daemon runtime",
    "ai.sensing": "sensing runtime",
    "ai.outreach_loop_daemon": "outreach loop daemon",
    "ai.self_repair_daemon": "self-repair daemon",
    "ai.corp_dashboard": "internal corp dashboard",
    "ai.thinktank_pipeline": "ThinkTank ideation pipeline",
    "ai.inbox_drafts": "inbox draft store",
    "ai.workers.executor": "worker executor",
    "ai.content_intel.sensor": "content intelligence sensor",
    "ai.session_phoenix": "session revival runtime",
}


def capability_unavailable(tool: str, module: str,
                           detail: str = "") -> Dict[str, Any]:
    """The structured no-op an INTERNAL-backed tool returns on a public install.

    Deliberately NOT an empty success: a caller must be able to tell
    "this capability is not in this distribution" apart from "this ran and
    found nothing", which are very different facts.
    """
    label = INTERNAL_BACKENDS.get(module, module)
    return {
        "status": "capability_unavailable",
        "tool": tool,
        "capability": label,
        "available": False,
        "reason": (
            f"'{tool}' is backed by {label}, an internal Delimit operations "
            f"component that is not part of the published package."
        ),
        "detail": detail or None,
        "what_to_use_instead": (
            "This is not a licensing state and upgrading does not enable it. "
            "Run `delimit scan` or `delimit quickstart` for the supported "
            "governance workflow."
        ),
    }


def guard_internal(tool: str, module: str, exc: Optional[BaseException] = None):
    """Translate a missing INTERNAL backend into a structured result.

    Returns the capability_unavailable payload when `module` is a known
    INTERNAL backend. Returns None when it is NOT — an unknown missing module
    is a real packaging defect and must keep raising, so the next regression
    of this class is loud instead of silently absorbed.
    """
    if module in INTERNAL_BACKENDS:
        return capability_unavailable(tool, module, detail=str(exc) if exc else "")
    return None


def missing_module_name(exc: BaseException) -> str:
    """Best-effort module name from a ModuleNotFoundError."""
    name = getattr(exc, "name", None)
    if name:
        return str(name)
    text = str(exc)
    if "'" in text:
        try:
            return text.split("'")[1]
        except IndexError:
            pass
    return ""
