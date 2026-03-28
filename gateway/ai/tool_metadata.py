"""
Tool Metadata — Tier mappings for all Delimit MCP tools.

Each tool is assigned a visibility tier:
  - "public":       Always visible to all MCP clients.
  - "ops_pack":     Visible when DELIMIT_SHOW_OPS=1 (or SHOW_INTERNAL/SHOW_EXPERIMENTAL).
  - "internal":     Visible when DELIMIT_SHOW_INTERNAL=1 (or SHOW_EXPERIMENTAL).
  - "experimental": Visible when DELIMIT_SHOW_EXPERIMENTAL=1.

Tier cascade: experimental > internal > ops_pack > public.

Reference: Consensus 118/119/120 — Tool Segmentation Architecture.
"""

from typing import Dict, Literal

Tier = Literal["public", "ops_pack", "internal", "experimental"]

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
    # === Govern domain (all public) ===
    "delimit_lint": "public",
    "delimit_diff": "public",
    "delimit_policy": "public",
    "delimit_semver": "public",
    "delimit_explain": "public",
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
