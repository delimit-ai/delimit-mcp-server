"""
Tool Metadata — Tier mappings for all Delimit MCP tools.

Each tool is assigned a visibility tier:
  - "public":       Always visible to all MCP clients.
  - "ops_pack":     Visible when DELIMIT_SHOW_OPS=1 (or SHOW_INTERNAL/SHOW_EXPERIMENTAL).
  - "internal":     Visible when DELIMIT_SHOW_INTERNAL=1 (or SHOW_EXPERIMENTAL).
  - "experimental": Visible when DELIMIT_SHOW_EXPERIMENTAL=1.

Tier cascade: experimental > internal > ops_pack > public.

Reference: Consensus 118/119/120 — Tool Segmentation Architecture.

The records profile serves a Claude plugin's explicit decision and handoff
records. It excludes memory, soul and session capture, transcript extraction
(session_handoff reads transcript/git state), deliberation (model providers),
notify/social/outreach (network), security audit (auto-notifies), and all
Pro, experimental and internal tools.
"""

from typing import Dict, Literal

Tier = Literal["core", "public", "ops_pack", "internal", "experimental"]

# ─────────────────────────────────────────────────────────────────────
#  CORE WORKFLOWS — the 5 outcomes first users should see
#
#  1. Govern: catch breaking changes before deploy
#  2. Remember: persistent memory across models and sessions
#  3. Handoff: switch models without losing context
#  4. Lint: validate API specs for drift
#  5. Track: ledger for tasks that survive context resets
#
#  Per consensus (STR-040): sell 5 workflows, not 162 tools
# ─────────────────────────────────────────────────────────────────────

CORE_TOOLS = {
    # Govern
    "delimit_lint", "delimit_scan", "delimit_gov_health", "delimit_drift_check",
    # Remember
    "delimit_memory_store", "delimit_memory_search", "delimit_memory_recent",
    # Handoff
    "delimit_session_handoff", "delimit_session_history",
    # Lint + Diff
    "delimit_diff", "delimit_semver", "delimit_explain",
    # Track
    "delimit_ledger_context", "delimit_ledger_add", "delimit_ledger_done",
    "delimit_ledger_list", "delimit_ledger_update",
    # Setup
    "delimit_init", "delimit_scan", "delimit_version", "delimit_help",
}

# ─────────────────────────────────────────────────────────────────────
#  TOOL_TIERS: canonical tier assignment for every registered tool.
#
#  ops_pack (~8):  workspace automation tools (social, notify, design
#                  validation, screenshots, screen recording, docs gen)
#
#  internal (~18): founder-only tools (content pipeline, design internals,
#                  storybook, data ops, social management, scaffolding)
#
#  experimental:   stubs and pass-throughs hidden behind LED-044
#
#  public:         everything else (the default)
# ─────────────────────────────────────────────────────────────────────

TOOL_TIERS: Dict[str, Tier] = {
    # === Govern domain ===
    "delimit_lint": "core",
    "delimit_diff": "core",
    "delimit_policy": "public",
    "delimit_semver": "core",
    "delimit_explain": "core",
    "delimit_zero_spec": "public",
    "delimit_init": "public",
    "delimit_gov_health": "public",
    "delimit_gov_status": "public",
    "delimit_gov_policy": "public",
    "delimit_gov_evaluate": "public",
    "delimit_gov_new_task": "public",
    "delimit_gov_run": "public",
    "delimit_gov_verify": "public",
    "delimit_ledger": "public",
    "delimit_impact": "public",

    # === Context domain (all public) ===
    "delimit_memory_search": "public",
    "delimit_memory_store": "public",
    "delimit_memory_recent": "public",
    "delimit_vault_search": "public",
    "delimit_vault_health": "public",
    "delimit_vault_snapshot": "public",
    "delimit_context_init": "public",
    "delimit_context_write": "public",
    "delimit_context_read": "public",
    "delimit_context_list": "public",
    "delimit_context_snapshot": "public",
    "delimit_context_branch": "public",
    "delimit_secret_store": "public",
    "delimit_secret_get": "public",
    "delimit_secret_list": "public",
    "delimit_secret_revoke": "public",
    "delimit_secret_access_log": "public",
    "delimit_seal_verify": "public",  # open-core Seal receipt verifier (Free tier)

    # === Ship domain (public + experimental) ===
    "delimit_deploy_plan": "public",
    "delimit_deploy_build": "public",
    "delimit_deploy_publish": "public",
    "delimit_deploy_verify": "experimental",
    "delimit_deploy_rollback": "public",
    "delimit_deploy_status": "public",
    "delimit_deploy_site": "public",
    "delimit_deploy_npm": "public",
    "delimit_release_plan": "public",
    "delimit_release_validate": "experimental",
    "delimit_release_status": "public",
    "delimit_release_rollback": "experimental",
    "delimit_release_history": "experimental",
    "delimit_release_sync": "public",

    # === Observe domain (public + experimental) ===
    "delimit_obs_metrics": "public",
    "delimit_obs_logs": "public",
    "delimit_obs_alerts": "experimental",
    "delimit_obs_status": "public",
    "delimit_cost_analyze": "public",
    "delimit_cost_optimize": "public",
    "delimit_cost_alert": "public",
    "delimit_cost_controls": "public",

    # === Security domain (all public) ===
    "delimit_security_scan": "public",
    "delimit_security_ingest": "public",
    "delimit_security_deliberate": "public",
    "delimit_security_audit": "public",
    "delimit_evidence_collect": "public",
    "delimit_evidence_verify": "public",

    # === Intel domain (all public) ===
    "delimit_intel_dataset_register": "public",
    "delimit_intel_dataset_list": "public",
    "delimit_intel_dataset_freeze": "public",
    "delimit_intel_snapshot_ingest": "public",
    "delimit_intel_query": "public",

    # === Repo domain (experimental stubs) ===
    "delimit_repo_diagnose": "experimental",
    "delimit_repo_analyze": "experimental",
    "delimit_repo_config_validate": "experimental",
    "delimit_repo_config_audit": "experimental",

    # === Test domain (public + experimental) ===
    "delimit_test_generate": "public",
    "delimit_test_coverage": "experimental",
    "delimit_test_smoke": "public",

    # === Orchestrate domain (all public) ===
    "delimit_os_plan": "public",
    "delimit_os_status": "public",
    "delimit_os_gates": "public",
    "delimit_ledger_add": "public",
    "delimit_ledger_done": "public",
    "delimit_ledger_list": "public",
    "delimit_ledger_context": "public",
    "delimit_ventures": "public",
    "delimit_models": "public",
    "delimit_deliberate": "public",
    "delimit_sensor_github_issue": "public",
    "delimit_resource_list": "public",
    "delimit_resource_get": "public",
    "delimit_resource_drivers": "public",
    "delimit_tracker_sync": "public",
    "delimit_webhook_manage": "public",
    "delimit_agent_dispatch": "public",
    "delimit_agent_status": "public",
    "delimit_agent_complete": "public",
    "delimit_agent_handoff": "public",
    "delimit_next_task": "public",
    "delimit_task_complete": "public",
    "delimit_loop_status": "public",
    "delimit_loop_config": "public",
    "delimit_daemon_status": "public",
    "delimit_daemon_run": "public",
    "delimit_daemon_classify": "public",
    "delimit_config_export": "public",
    "delimit_config_import": "public",
    "delimit_changelog": "public",

    # === Utility (all public) ===
    "delimit_version": "public",
    "delimit_help": "public",
    "delimit_diagnose": "public",
    "delimit_activate": "public",
    "delimit_license_status": "public",
    "delimit_scan": "public",
    "delimit_quickstart": "public",

    # ═══════════════════════════════════════════════════════════════
    #  Workspace-ops tier (~8 tools)
    #  Visible with DELIMIT_SHOW_OPS=1 or DELIMIT_SHOW_INTERNAL=1
    # ═══════════════════════════════════════════════════════════════
    "delimit_social_post": "ops_pack",
    "delimit_social_generate": "ops_pack",
    "delimit_social_approve": "ops_pack",
    "delimit_notify": "ops_pack",
    "delimit_design_validate_responsive": "ops_pack",
    "delimit_screenshot": "ops_pack",
    "delimit_screen_record": "ops_pack",
    "delimit_docs_generate": "ops_pack",

    # ═══════════════════════════════════════════════════════════════
    #  Internal tier (~18 tools)
    #  Visible with DELIMIT_SHOW_INTERNAL=1
    # ═══════════════════════════════════════════════════════════════
    "delimit_content_schedule": "internal",
    "delimit_content_publish": "internal",
    "delimit_content_queue": "internal",
    "delimit_inbox_daemon": "internal",
    "delimit_notify_inbox": "internal",
    "delimit_design_generate_component": "internal",
    "delimit_design_extract_tokens": "internal",
    "delimit_design_component_library": "internal",
    "delimit_design_generate_tailwind": "internal",
    "delimit_story_generate": "internal",
    "delimit_story_build": "internal",
    "delimit_story_visual_test": "internal",
    "delimit_story_accessibility": "internal",
    "delimit_data_backup": "internal",
    "delimit_data_migrate": "internal",
    "delimit_data_validate": "internal",
    "delimit_social_history": "internal",
    "delimit_social_accounts": "internal",
    "delimit_generate_scaffold": "internal",
    "delimit_generate_template": "internal",
}

# Founder-operated workflows that predate the tier metadata.  This is metadata
# for future public surfaces; the compatibility profiles below retain their
# historical membership so installed users keep the same tool lists.
FOUNDER_TOOLS = frozenset({
    "delimit_think", "delimit_build", "delimit_build_loop",
    "delimit_build_loop_daemon", "delimit_swarm",
    "delimit_social_post", "delimit_social_approve",
    "delimit_social_daemon", "delimit_social_target",
    "delimit_social_target_config", "delimit_x_fetch",
    "delimit_outreach_loop_tick", "delimit_vendor_news_scan",
    "delimit_vendor_news_health", "delimit_vendor_news_draft",
})
TOOL_TIERS.update({name: "internal" for name in FOUNDER_TOOLS})


# ─────────────────────────────────────────────────────────────────────
#  DELIMIT_TOOLSET — opt-in compatibility profiles (LED-3709)
#
#  Additive, non-breaking. Selected via the DELIMIT_TOOLSET env var:
#
#    full  (default / unset) → every tool registers, byte-identical to
#                              the historical surface. Pro customers have
#                              this server installed locally; default-full
#                              guarantees zero breakage.
#    standard                → drops the founder-only "internal" and
#                              "experimental" tiers; keeps core + public +
#                              workspace-ops. Intermediate surface.
#    core                    → only the essential merge-gate + memory +
#                              ledger + handoff set (see CORE_SET below).
#                              Sized for clients with tight tool caps
#                              (e.g. VS Code agent mode's 128-tool limit).
#    records                 → explicit decisions, handoffs, and local memory
#                              for a Claude plugin. Excludes soul and session
#                              capture, transcript extraction (session_handoff
#                              reads transcripts/git state), deliberation
#                              (model providers), notify/social/outreach
#                              (network), security audit (auto-notifies), and
#                              all other Pro, experimental and internal tools.
#    governance              → local API spec checks (URL lint inputs opt in to
#                              fetching the supplied URL).
#    panel                   → opt-in multi-model deliberation and model setup.
#
#  This reuses the existing TOOL_TIERS classification and CORE_TOOLS set;
#  it does NOT introduce a parallel taxonomy. Where CORE_TOOLS was too
#  thin for a usable "core" profile it is EXTENDED additively below (see
#  CORE_PROFILE_EXTRA) — the original CORE_TOOLS set is left untouched so
#  the "5 workflows" documentation stays accurate.
# ─────────────────────────────────────────────────────────────────────

VALID_TOOLSETS = ("core", "standard", "full", "records", "governance", "panel", "assistant", "lead", "frontend", "worker")
DEFAULT_TOOLSET = "full"
# Assistant-profile caps (unchanged). The armed lead profile uses LEAD_LIMITS;
# fin_lead.limits() selects between them at call time.
PROPOSE_24H_LIMIT = 20
RECORDS_24H_LIMIT = 60
RECORDS_MINUTE_LIMIT = 10
THINK_24H_LIMIT = 20
LEAD_LIMITS = {"propose": 100, "records_24h": 300, "records_minute": 30, "think": 20}

# LED-5835: an explicitly set but unrecognized DELIMIT_TOOLSET value used to
# resolve to "full", so a typo'd reduced profile silently exposed every tool.
# It now fails CLOSED to this profile, which registers only version/help.
# Unset / empty still resolves to "full" (existing installs unchanged).
FAIL_CLOSED_TOOLSET = "none"
FAIL_CLOSED_TOOLS = frozenset({"delimit_version", "delimit_help"})
ASSISTANT_TOOLS = frozenset({"delimit_brief", "delimit_item", "delimit_item_history", "delimit_search", "delimit_pending_handoffs",
                             "delimit_version", "delimit_help", "delimit_propose", "delimit_review_queue",
                             "delimit_mandate", "delimit_capabilities", "delimit_ledger_write", "delimit_deliberate_lite",
                             "delimit_capture", "delimit_checkpoint_write", "delimit_checkpoint_read", "delimit_workstreams",
                             "delimit_start_here"})
# Panel tools also exposed to the assistant toolset (Fin / lead front-end operators).
# These are NOT in ASSISTANT_TOOLS so they remain available in other profiles too.
ASSISTANT_PANEL_TOOLS = frozenset({"delimit_deliberate", "delimit_deliberation_status"})
_ASSISTANT_ONLY = ASSISTANT_TOOLS - FAIL_CLOSED_TOOLS
LEAD_EXCLUDED_TOOLS = frozenset({"delimit_secret_get", "delimit_secret_store", "delimit_secret_revoke"})
# Status tools a disarmed lead may still call, so it can say why it is disarmed.
LEAD_STATUS_TOOLS = frozenset({"delimit_capabilities", "delimit_start_here", "delimit_mandate", "delimit_version"})


def lead_excluded(tool_name: str) -> bool:
    """Secret values never reach a front-end transcript: every secret tool, including
    the unified _delimit_secret_impl dispatcher and metadata listings, is excluded."""
    return tool_name in LEAD_EXCLUDED_TOOLS or "secret" in tool_name.lower()

RECORDS_TOOLS = frozenset({
    "delimit_ledger_add", "delimit_ledger_list", "delimit_ledger_update",
    "delimit_ledger_done", "delimit_ledger_context",
    "delimit_handoff_create", "delimit_handoff_list",
    "delimit_handoff_acknowledge", "delimit_version", "delimit_help",
    "delimit_memory_store", "delimit_memory_search", "delimit_memory_recent",
})

GOVERNANCE_TOOLS = frozenset({
    "delimit_lint", "delimit_diff", "delimit_semver", "delimit_spec_health",
    "delimit_explain", "delimit_drift_check", "delimit_version", "delimit_help",
})

PANEL_TOOLS = frozenset({
    "delimit_deliberate", "delimit_deliberation_status", "delimit_models",
    "delimit_version", "delimit_help",
})

# Essential tools added to the core profile on top of CORE_TOOLS. These are
# the merge-gate + governance + continuity surfaces the audit (LED-3709)
# identified as the empirical/essential core but which predate the
# "5 workflows" CORE_TOOLS list. Additive only.
CORE_PROFILE_EXTRA = {
    "delimit_deliberate",       # multi-model consensus (moat)
    "delimit_revive",           # session revival / continuity
    "delimit_soul_capture",     # session capture / continuity
    "delimit_gov_evaluate",     # policy evaluation
    "delimit_security_audit",   # merge-gate: security
    "delimit_test_smoke",       # merge-gate: tests actually ran
    "delimit_evidence_collect", # attestation / audit trail
    "delimit_context_read",     # cross-session context
    "delimit_context_write",
    "delimit_diagnose",         # setup/health (doctor)
    "delimit_handoff_create",   # model handoff
}

# Tiers excluded from the "standard" profile.
_STANDARD_EXCLUDED_TIERS = frozenset({"internal", "experimental"})
_STANDARD_LEGACY_INCLUDED = FOUNDER_TOOLS


def core_tool_set() -> set:
    """The set of tool names visible under DELIMIT_TOOLSET=core.

    Union of CORE_TOOLS (the "5 workflows" set), any tool explicitly
    tier-classified "core" in TOOL_TIERS, and CORE_PROFILE_EXTRA.
    """
    core = set(CORE_TOOLS)
    core |= {name for name, tier in TOOL_TIERS.items() if tier == "core"}
    core |= CORE_PROFILE_EXTRA
    return core


def tool_in_toolset(tool_name: str, toolset: str) -> bool:
    """Return True if ``tool_name`` should register under ``toolset``.

    Fails CLOSED (LED-5835): an unknown toolset value exposes nothing
    beyond the fail-closed profile, so a misconfigured reduced profile can
    never widen the surface to every tool.
    """
    if toolset in {"frontend", "worker"}:
        from ai.frontend.registration import MODULE_MANIFEST
        return tool_name in MODULE_MANIFEST[toolset]
    if toolset == "assistant":
        return tool_name in ASSISTANT_TOOLS or tool_name in ASSISTANT_PANEL_TOOLS
    if toolset == "lead":
        return not lead_excluded(tool_name)
    if tool_name in _ASSISTANT_ONLY:
        return False
    if toolset == "full":
        return True
    if toolset == "core":
        return tool_name in core_tool_set()
    if toolset == "records":
        return tool_name in RECORDS_TOOLS
    if toolset == "governance":
        return tool_name in GOVERNANCE_TOOLS
    if toolset == "panel":
        return tool_name in PANEL_TOOLS
    if toolset == FAIL_CLOSED_TOOLSET:
        return tool_name in FAIL_CLOSED_TOOLS
    if toolset == "standard":
        if tool_name in _STANDARD_LEGACY_INCLUDED:
            return True
        tier = TOOL_TIERS.get(tool_name, "public")
        return tier not in _STANDARD_EXCLUDED_TIERS
    # Unknown / misconfigured value → fail closed (LED-5835).
    return tool_name in FAIL_CLOSED_TOOLS


def resolve_toolset(raw: str | None) -> str:
    """Normalize a raw DELIMIT_TOOLSET env value to a valid toolset name.

    Unset or empty resolves to the default ("full"), preserving
    byte-identical default behavior. An explicitly set but unrecognized
    value fails CLOSED to FAIL_CLOSED_TOOLSET and warns on stderr
    (LED-5835); stdout is never used because it carries MCP stdio.
    """
    if not raw or not raw.strip():
        return DEFAULT_TOOLSET
    val = raw.strip().lower()
    if val in VALID_TOOLSETS or val == FAIL_CLOSED_TOOLSET:
        return val
    import sys
    print(f"delimit: unrecognized DELIMIT_TOOLSET={raw.strip()!r}; exposing only "
          f"{', '.join(sorted(FAIL_CLOSED_TOOLS))}. Valid: {', '.join(VALID_TOOLSETS)}.",
          file=sys.stderr)
    return FAIL_CLOSED_TOOLSET
