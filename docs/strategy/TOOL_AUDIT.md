# Toolset Audit & Consolidation Plan

## Target Tool Count: ~45 (Reduced from 187)
**Justification:** A registry of 187 tools overwhelms LLM context windows, increases token costs, and degrades tool-selection accuracy. We must consolidate hyper-specific CRUD tools into unified orchestrators.

## Execute/Deploy-Class Permission Gating Proposal
All tools capable of modifying external state, deploying code, or posting publicly must be moved to a strict **Tier 2 Permission Gate**. They must require either an explicit `DELIMIT_ALLOW_EXECUTE=1` environment variable or interactive user approval per run.
**Flagged Tools:** `delimit_deploy_plan`, `delimit_deploy_build`, `delimit_deploy_publish`, `delimit_deploy_rollback`, `delimit_release_rollback`, `delimit_webhook_manage`, `delimit_social_post`, `delimit_content_publish`, `delimit_daemon_run`, `delimit_build_loop_daemon`.

## Classification & Audit Table

| Category / Tools | Verdict | Justification |
|------------------|---------|---------------|
| **Core API Gov** (`delimit_lint`, `diff`, `semver`) | **KEEP** | Core differentiator. High use, low security surface. |
| **Agent OS** (`delimit_deliberate`, `delimit_ledger_*`) | **KEEP** | Defines the persistent, cross-model AI experience. |
| **Repo Scanners** (`delimit_repo_diagnose`, `_analyze`, `_config_*`) | **MERGE** | Consolidate into a single `delimit_repo_audit` tool with argument flags. |
| **Agent Sub-ops** (`delimit_agent_status`, `_complete`, `_link`) | **MERGE** | Consolidate into `delimit_agent_manage` and `delimit_agent_query`. |
| **Design/UI** (`delimit_design_*`, `delimit_story_*`) | **DEPRECATE** | Low differentiation vs specialized UI tools (e.g., v0, storybook native). |
| **Daemons** (`delimit_daemon_*`, `build_loop_*`) | **DEPRECATE** | Autonomous daemons violate user safety rules and have extreme execution risk. |
| **Social/News** (`vendor_news_*`, `social_history`) | **DEPRECATE** | Distracts from the core engineering wedge. |
| **Evidence** (`delimit_evidence_collect`, `seal_verify`) | **KEEP** | Forms the cryptographic attestation wedge. |
