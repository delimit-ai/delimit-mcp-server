"""
Delimit API Change Explainer

7 templates that transform raw diff/semver data into human-readable explanations
for different audiences and delivery channels.

Templates:
  1. developer   — Technical, code-focused detail
  2. team_lead   — Executive summary for tech leads
  3. product     — Business-impact focus for PMs
  4. migration   — Step-by-step migration guide
  5. changelog   — CHANGELOG.md entry
  6. pr_comment  — GitHub PR comment (compact markdown)
  7. slack       — Slack notification (mrkdwn)
"""

from typing import Any, Dict, List, Optional

from .diff_engine_v2 import Change, ChangeType
from .semver_classifier import SemverBump, classify, classify_detailed


# ── Public API ────────────────────────────────────────────────────────

TEMPLATES = [
    "developer",
    "team_lead",
    "product",
    "migration",
    "changelog",
    "pr_comment",
    "slack",
]


def explain(
    changes: List[Change],
    template: str = "developer",
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> str:
    """Generate a human-readable explanation of API changes.

    Args:
        changes: List of Change objects from the diff engine.
        template: One of the 7 template names.
        old_version: Previous API version (e.g. "1.0.0").
        new_version: New API version (e.g. "2.0.0").
        api_name: Optional API/service name for context.

    Returns:
        Formatted explanation string.
    """
    detail = classify_detailed(changes)
    ctx = _build_context(detail, changes, old_version, new_version, api_name)

    renderer = _RENDERERS.get(template)
    if renderer is None:
        return f"Unknown template '{template}'. Available: {', '.join(TEMPLATES)}"
    return renderer(ctx)


def explain_all(
    changes: List[Change],
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, str]:
    """Generate all 7 template outputs at once."""
    detail = classify_detailed(changes)
    ctx = _build_context(detail, changes, old_version, new_version, api_name)
    return {name: _RENDERERS[name](ctx) for name in TEMPLATES}


# ── Internal context builder ──────────────────────────────────────────

def _build_context(
    detail: Dict[str, Any],
    changes: List[Change],
    old_version: Optional[str],
    new_version: Optional[str],
    api_name: Optional[str],
) -> Dict[str, Any]:
    return {
        **detail,
        "changes": changes,
        "old_version": old_version or "unknown",
        "new_version": new_version or "unknown",
        "api_name": api_name or "API",
        "version_label": _version_label(old_version, new_version),
    }


def _version_label(old: Optional[str], new: Optional[str]) -> str:
    if old and new:
        return f"{old} -> {new}"
    return ""


# ── Renderers ─────────────────────────────────────────────────────────

def _render_developer(ctx: Dict) -> str:
    lines: List[str] = []
    bump = ctx["bump"]
    api = ctx["api_name"]
    ver = ctx["version_label"]

    lines.append(f"# {api} — Semver: {bump.upper()}" + (f" ({ver})" if ver else ""))
    lines.append("")

    if ctx["counts"]["breaking"] > 0:
        lines.append(f"## Breaking Changes ({ctx['counts']['breaking']})")
        lines.append("")
        for c in ctx["breaking_changes"]:
            lines.append(f"  - [{c['type']}] {c['message']}")
        lines.append("")

    if ctx["counts"]["additive"] > 0:
        lines.append(f"## Additions ({ctx['counts']['additive']})")
        lines.append("")
        for c in ctx["additive_changes"]:
            lines.append(f"  - [{c['type']}] {c['message']}")
        lines.append("")

    if ctx["counts"]["patch"] > 0:
        lines.append(f"## Patches ({ctx['counts']['patch']})")
        lines.append("")
        for c in ctx["patch_changes"]:
            lines.append(f"  - [{c['type']}] {c['message']}")
        lines.append("")

    lines.append(f"Total changes: {ctx['counts']['total']}")
    return "\n".join(lines)


def _render_team_lead(ctx: Dict) -> str:
    lines: List[str] = []
    bump = ctx["bump"]
    api = ctx["api_name"]
    ver = ctx["version_label"]
    bc = ctx["counts"]["breaking"]

    lines.append(f"## {api} Change Summary" + (f" ({ver})" if ver else ""))
    lines.append("")
    lines.append(f"**Recommended bump**: `{bump}`")
    lines.append(f"**Total changes**: {ctx['counts']['total']}")
    lines.append(f"**Breaking**: {bc}")
    lines.append(f"**Additive**: {ctx['counts']['additive']}")
    lines.append("")

    if bc > 0:
        lines.append("### Action required")
        lines.append("")
        lines.append("Breaking changes detected. Consumer teams must be notified before release.")
        lines.append("")
        for c in ctx["breaking_changes"]:
            lines.append(f"- {c['message']}")
    else:
        lines.append("No breaking changes. Safe to release without consumer coordination.")

    return "\n".join(lines)


def _render_product(ctx: Dict) -> str:
    lines: List[str] = []
    api = ctx["api_name"]
    bump = ctx["bump"]
    bc = ctx["counts"]["breaking"]
    add = ctx["counts"]["additive"]

    lines.append(f"## {api} — Impact Assessment")
    lines.append("")

    if bc > 0:
        lines.append(f"**Risk level**: HIGH — {bc} breaking change(s) detected.")
        lines.append("")
        lines.append("**What this means**: Existing integrations will break if these changes ship")
        lines.append("without a coordinated migration. Downstream partners and client teams")
        lines.append("need advance notice.")
        lines.append("")
        lines.append("**Breaking changes**:")
        for c in ctx["breaking_changes"]:
            lines.append(f"  - {c['message']}")
    elif add > 0:
        lines.append("**Risk level**: LOW — New capabilities added, no existing behavior changed.")
        lines.append("")
        lines.append("**What this means**: New features available. Existing integrations unaffected.")
    else:
        lines.append("**Risk level**: NONE — Documentation or cosmetic changes only.")

    lines.append("")
    lines.append(f"**Recommended version bump**: `{bump}`")
    return "\n".join(lines)


def _render_migration(ctx: Dict) -> str:
    lines: List[str] = []
    api = ctx["api_name"]
    ver = ctx["version_label"]

    lines.append(f"# Migration Guide: {api}" + (f" ({ver})" if ver else ""))
    lines.append("")

    breaking: List[Dict] = ctx["breaking_changes"]
    if not breaking:
        lines.append("No breaking changes. No migration needed.")
        return "\n".join(lines)

    lines.append(f"This release contains **{len(breaking)} breaking change(s)**.")
    lines.append("Follow the steps below to update your integration.")
    lines.append("")

    for i, c in enumerate(breaking, 1):
        lines.append(f"### Step {i}: {c['type'].replace('_', ' ').title()}")
        lines.append("")
        lines.append(f"**Change**: {c['message']}")
        lines.append(f"**Location**: `{c['path']}`")
        lines.append("")
        lines.append(_migration_advice(c["type"]))
        lines.append("")

    lines.append("---")
    lines.append("After completing all steps, run your integration tests to verify.")
    return "\n".join(lines)


def _render_changelog(ctx: Dict) -> str:
    lines: List[str] = []
    ver = ctx.get("new_version") or "Unreleased"

    lines.append(f"## [{ver}]")
    lines.append("")

    if ctx["counts"]["breaking"] > 0:
        lines.append("### Breaking Changes")
        lines.append("")
        for c in ctx["breaking_changes"]:
            lines.append(f"- {c['message']}")
        lines.append("")

    if ctx["counts"]["additive"] > 0:
        lines.append("### Added")
        lines.append("")
        for c in ctx["additive_changes"]:
            lines.append(f"- {c['message']}")
        lines.append("")

    if ctx["counts"]["patch"] > 0:
        lines.append("### Changed")
        lines.append("")
        for c in ctx["patch_changes"]:
            lines.append(f"- {c['message']}")
        lines.append("")

    return "\n".join(lines)


def _render_pr_comment(ctx: Dict) -> str:
    lines: List[str] = []
    bump = ctx["bump"]
    bc = ctx["counts"]["breaking"]
    total = ctx["counts"]["total"]
    additive_count = ctx["counts"]["additive"]

    if bc == 0:
        # ── GREEN PATH ──
        semver_label = bump.upper() if bump != "none" else "NONE"
        lines.append("## \U0001f6e1\ufe0f Governance Passed")
        lines.append("")
        if total > 0:
            lines.append(
                f"> **No breaking API changes detected.** "
                f"{additive_count} additive change{'s' if additive_count != 1 else ''} "
                f"found \u2014 Semver: **{semver_label}**"
            )
        else:
            lines.append("> **No breaking API changes detected.**")
        lines.append("")

        # Additive changes (collapsed)
        additive = ctx["additive_changes"]
        if additive:
            lines.append("<details>")
            lines.append(f"<summary>\u2705 New additions ({len(additive)})</summary>")
            lines.append("")
            for c in additive:
                lines.append(f"- `{c['path']}` \u2014 {c['message']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    else:
        # ── RED PATH ──
        lines.append("## \U0001f6e1\ufe0f Breaking API Changes Detected")
        lines.append("")

        # Summary card
        parts = [f"\U0001f534 **{bc} breaking change{'s' if bc != 1 else ''}**"]
        parts.append("Semver: **MAJOR**")
        separator = " \u00b7 "
        lines.append(f"> {separator.join(parts)}")
        lines.append("")

        # Stats table
        lines.append("| | Count |")
        lines.append("|---|---|")
        lines.append(f"| Total changes | {total} |")
        lines.append(f"| Breaking | {bc} |")
        lines.append(f"| Additive | {additive_count} |")
        lines.append("")

        # Breaking changes table
        lines.append("### Breaking Changes")
        lines.append("")
        lines.append("| Severity | Change | Location |")
        lines.append("|----------|--------|----------|")
        for c in ctx["breaking_changes"]:
            change_type = c.get("type", "breaking")
            severity = _pr_severity(change_type)
            lines.append(f"| {severity} | {c['message']} | `{c['path']}` |")
        lines.append("")

        # Migration guidance
        lines.append("<details>")
        lines.append("<summary>\U0001f4cb Migration guide</summary>")
        lines.append("")
        for i, c in enumerate(ctx["breaking_changes"], 1):
            lines.append(f"**{i}. `{c['path']}`**")
            lines.append(f"{_pr_migration_hint(c)}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

        # Additive changes
        additive = ctx["additive_changes"]
        if additive:
            lines.append("<details>")
            lines.append(f"<summary>\u2705 New additions ({len(additive)})</summary>")
            lines.append("")
            for c in additive:
                lines.append(f"- `{c['path']}` \u2014 {c['message']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("> **Fix locally:** `npx delimit-cli lint`")
        lines.append("")

    lines.append("---")
    lines.append(
        "Powered by [Delimit](https://delimit.ai) \u00b7 "
        "[Docs](https://delimit.ai/docs) \u00b7 "
        "[Install](https://github.com/marketplace/actions/delimit-api-governance)"
    )
    return "\n".join(lines)


def _pr_severity(change_type: str) -> str:
    """Map change type to severity emoji for PR comments."""
    critical = {"endpoint_removed", "method_removed", "field_removed"}
    high = {"required_param_added", "type_changed", "enum_value_removed"}
    if change_type in critical:
        return "🔴 Critical"
    if change_type in high:
        return "🟠 High"
    return "🟡 Medium"


def _pr_migration_hint(change: Dict) -> str:
    """Generate a migration hint for a breaking change."""
    ct = change.get("type", "")
    if ct == "endpoint_removed":
        return "Consumers must stop calling this endpoint. Consider a deprecation period."
    if ct == "method_removed":
        return "Consumers using this HTTP method must migrate to an alternative."
    if ct == "required_param_added":
        return "All existing consumers must include this parameter. Consider making it optional with a default."
    if ct == "field_removed":
        return "Consumers reading this field will break. Add it back or provide a migration path."
    if ct == "type_changed":
        return "Consumers expecting the old type will fail to parse. Coordinate the type migration."
    if ct == "enum_value_removed":
        return "Consumers using this value must update. Consider keeping it as deprecated."
    return "Review this change and update consumers accordingly."


def _render_slack(ctx: Dict) -> str:
    bump = ctx["bump"]
    api = ctx["api_name"]
    bc = ctx["counts"]["breaking"]
    total = ctx["counts"]["total"]
    ver = ctx["version_label"]

    icon = ":red_circle:" if bc > 0 else ":large_green_circle:"

    lines: List[str] = []
    lines.append(f"{icon} *{api} API Change* — `{bump}` bump" + (f" ({ver})" if ver else ""))
    lines.append("")
    lines.append(f"Changes: {total} total, {bc} breaking, {ctx['counts']['additive']} additive")

    if bc > 0:
        lines.append("")
        lines.append("*Breaking:*")
        for c in ctx["breaking_changes"][:5]:  # cap at 5 for Slack
            lines.append(f"  > {c['message']}")
        if bc > 5:
            lines.append(f"  > ...and {bc - 5} more")

    return "\n".join(lines)


# ── Migration advice per change type ─────────────────────────────────

def _migration_advice(change_type: str) -> str:
    advice = {
        "endpoint_removed": (
            "**Action**: Update all clients to stop calling this endpoint. "
            "If you control the consumers, search for references and remove them. "
            "Consider using the new endpoint (if applicable) as a replacement."
        ),
        "method_removed": (
            "**Action**: Update clients using this HTTP method. "
            "Check if an alternative method is available on the same path."
        ),
        "required_param_added": (
            "**Action**: All existing requests must now include this parameter. "
            "Update every call site to pass the new required value."
        ),
        "param_removed": (
            "**Action**: Remove this parameter from all requests. "
            "Sending it may cause errors or be silently ignored."
        ),
        "response_removed": (
            "**Action**: Update any client logic that depends on this response code. "
            "Check what the new expected response is."
        ),
        "required_field_added": (
            "**Action**: If this is a request body field, include it in all requests. "
            "If this is a response field, update parsers to handle the new field."
        ),
        "field_removed": (
            "**Action**: Remove any references to this field in your response parsers. "
            "Accessing it will return undefined/null."
        ),
        "type_changed": (
            "**Action**: Update serialization/deserialization logic for the new type. "
            "Check all type assertions, validators, and database column types."
        ),
        "format_changed": (
            "**Action**: Update parsing logic for the new format. "
            "For example, if a date field changed from 'date' to 'date-time'."
        ),
        "enum_value_removed": (
            "**Action**: Stop sending the removed enum value. "
            "Update any switch/case or if/else blocks that handle it."
        ),
    }
    return advice.get(change_type, "**Action**: Review the change and update your integration accordingly.")


# ── Renderer registry ─────────────────────────────────────────────────

_RENDERERS = {
    "developer": _render_developer,
    "team_lead": _render_team_lead,
    "product": _render_product,
    "migration": _render_migration,
    "changelog": _render_changelog,
    "pr_comment": _render_pr_comment,
    "slack": _render_slack,
}
