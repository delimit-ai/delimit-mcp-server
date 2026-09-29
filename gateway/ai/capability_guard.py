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
#
# STR-6400: the 4.19.10 tools/call sweep proved the pre-existing list was
# incomplete — 11 registered tools still raised unhandled ImportError /
# ModuleNotFoundError on the public bundle (ai.social x5, ai.swarm,
# ai.screen_record x2, ai.outreach_substantive, ai.workers, ai.loop_daemon).
# Every ai.* module imported by ai/server.py that bundle-internal-exclude.txt
# excludes is now listed explicitly (specific label), and guard_internal()
# additionally contains ANY other missing ai.* backend with a generic label
# so a future exclusion cannot reintroduce a customer-facing traceback.
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
    # STR-6400 additions: server.py backends excluded from the public bundle
    # that the 4.19.10 sweep caught raising (or one import away from raising).
    "ai.social": "social posting backend",
    "ai.swarm": "agent swarm runtime",
    "ai.screen_record": "screen recording backend",
    "ai.outreach_substantive": "outreach content evaluator",
    "ai.outreach_gate": "outreach gating backend",
    "ai.loop_daemon": "autonomous loop daemon",
    "ai.report_backlog": "report backlog store",
    "ai.workers": "worker executor",
    "ai.supabase_sync": "cloud dashboard sync",
    "ai.vendor_news.sensor": "vendor news sensing",
    "ai.vendor_news.drafter": "vendor news drafting",
    "ai.sensing.signal_store": "sensing runtime",
    "ai.sensing.schema": "sensing runtime",
}

# Required lazy backends for MCP tools.  Keep this explicit: some tools import
# an internal module only for an optional feature and have a shipped fallback
# (for example diagnose, notify, soul_capture, and revive).  Those tools must
# remain registered in a public install.
TOOL_REQUIRED_BACKENDS: Dict[str, tuple[str, ...]] = {
    "delimit_substantive_content_check": ("ai.outreach_substantive", "ai.outreach_gate"),
    "delimit_outreach_loop_tick": ("ai.outreach_loop_daemon",),
    "delimit_digest": ("ai.daily_digest",),
    "delimit_executor": ("ai.workers.executor",),
    "delimit_sense": ("ai.sensing",),
    "delimit_siem": ("ai.siem_streaming",),
    "delimit_sensor_github_migrations": ("ai.social_target",),
    "delimit_swarm": ("ai.swarm",),
    "delimit_x_fetch": ("ai.social_target",),
    "delimit_social_post": ("ai.social",),
    "delimit_social_generate": ("ai.social",),
    "delimit_social_accounts": ("ai.social",),
    "delimit_social_history": ("ai.social",),
    "delimit_social_approve": ("ai.social",),
    "delimit_social_target": ("ai.social_target", "ai.report_backlog"),
    "delimit_social_target_config": ("ai.social_target",),
    "delimit_reddit_scan": ("ai.reddit_scanner",),
    "delimit_github_scan": ("ai.github_scanner",),
    "delimit_vendor_news_scan": ("ai.vendor_news",),
    "delimit_vendor_news_health": ("ai.vendor_news.sensor", "ai.vendor_news.drafter"),
    "delimit_vendor_news_draft": ("ai.vendor_news", "ai.vendor_news.sensor", "ai.social_target"),
    "delimit_content_schedule": ("ai.content_engine",),
    "delimit_content_publish": ("ai.content_engine",),
    "delimit_content_queue": ("ai.content_engine",),
    "delimit_daemon_status": ("ai.daemon",),
    "delimit_daemon_run": ("ai.daemon",),
    "delimit_daemon_classify": ("ai.daemon",),
    "delimit_build_loop": ("ai.loop_engine",),
    "delimit_build_loop_daemon": ("ai.loop_daemon",),
    "delimit_inbox_daemon": ("ai.inbox_daemon",),
    "delimit_social_daemon": ("ai.social_daemon",),
    "delimit_self_repair_daemon": ("ai.self_repair_daemon",),
    "delimit_corp_dashboard": ("ai.corp_dashboard",),
    "delimit_screen_record": ("ai.screen_record",),
    "delimit_screenshot": ("ai.screen_record",),
    "delimit_next_task": ("ai.loop_engine",),
    "delimit_task_complete": ("ai.loop_engine",),
    "delimit_loop_status": ("ai.loop_engine",),
    "delimit_loop_config": ("ai.loop_engine",),
    "delimit_content_intel_daily": ("ai.content_intel",),
    "delimit_content_intel_topic": ("ai.content_intel",),
    "delimit_content_intel_weekly": ("ai.content_intel",),
    "delimit_reddit_fetch_thread": ("ai.reddit_scanner",),
    "delimit_think": ("ai.thinktank_pipeline",),
    "delimit_build": ("ai.thinktank_pipeline",),
}


def tool_backend_absent(tool: str) -> bool:
    """Whether a tool's required implementation is missing from this install."""
    return any(module_absent(module) for module in TOOL_REQUIRED_BACKENDS.get(tool, ()))

# Generic label for a missing ai.* backend with no explicit entry above.
# Deliberately vague: the module name is already public (it appears in the
# shipped ai/server.py import statements), but there is no need to repeat a
# proprietary name the customer cannot act on.
_GENERIC_BACKEND_LABEL = "internal backend component"


def capability_unavailable(tool: str, module: str,
                           detail: str = "") -> Dict[str, Any]:
    """The structured no-op an INTERNAL-backed tool returns on a public install.

    Deliberately NOT an empty success: a caller must be able to tell
    "this capability is not in this distribution" apart from "this ran and
    found nothing", which are very different facts.
    """
    label = INTERNAL_BACKENDS.get(module, _GENERIC_BACKEND_LABEL)
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
    INTERNAL backend. STR-6400: also contains ANY other missing ``ai.*``
    backend with a generic label — the public bundle excludes ~127 gateway
    files, so an unlisted-but-absent ai.* module is an excluded backend, not
    a customer-actionable defect, and must not surface as a traceback.
    Returns None only for non-ai missing modules (third-party packaging
    defects), which must keep raising so the next regression of that class
    stays loud instead of silently absorbed.
    """
    # PR #614 review: only a module that is genuinely ABSENT is an excluded
    # backend. An ImportError raised from a module that IS on disk (renamed
    # symbol, broken import inside it) is a real defect and must keep raising
    # instead of being mislabelled "not part of the published package".
    if not module_absent(module):
        return None
    if module in INTERNAL_BACKENDS:
        return capability_unavailable(tool, module, detail=str(exc) if exc else "")
    if module.startswith("ai."):
        return capability_unavailable(tool, module, detail=str(exc) if exc else "")
    return None


def module_absent(module: str) -> bool:
    """True only when `module` cannot be found on the import path."""
    import importlib.util as _ilu

    if not module:
        return False
    try:
        return _ilu.find_spec(module) is None
    except (ImportError, ValueError):
        # Parent package missing / invalid name: treat as absent.
        return True


def missing_module_name(exc: BaseException) -> str:
    """Best-effort module name from a ModuleNotFoundError/ImportError."""
    import re as _re

    name = getattr(exc, "name", None)
    text = str(exc)
    # `from ai import loop_daemon` on a bundle without ai/loop_daemon.py
    # raises ImportError (not ModuleNotFoundError) with name='ai' and
    # message "cannot import name 'loop_daemon' from 'ai'". Recover the
    # real missing submodule so the guard can match it.
    if not name or name == "ai":
        m = _re.search(r"cannot import name '([A-Za-z0-9_]+)' from 'ai'", text)
        if m:
            return f"ai.{m.group(1)}"
    if name:
        return str(name)
    if "'" in text:
        try:
            return text.split("'")[1]
        except IndexError:
            pass
    return ""
