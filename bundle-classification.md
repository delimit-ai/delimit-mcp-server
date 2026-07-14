# Bundle Classification — delimit-cli npm package (fail-closed allowlist)

Derived from `gateway/ai/server.py` (the public MCP entrypoint) import analysis +
per-file nature review. PUBLIC = shipped to npm/public repo; INTERNAL = excluded.
Rule applied: **when uncertain, INTERNAL-EXCLUDE.** Import-safety verified: no PUBLIC
`.py` has a top-level import of an INTERNAL `.py` (lazy imports inside tool bodies are
fine — those internal tools simply no-op on a public install).

Counts: **PUBLIC 135** source paths (+1 compiled `.so`) · **INTERNAL 147** paths.

## FLAGGED FOR REVIEW (kept PUBLIC but borderline)
These are Delimit *product* tools (the multi-model / governance surface), not
cross-venture or proprietary, so they ship — but confirm they should be public:

- `gateway/ai/agent_dispatch.py` — kept PUBLIC (product surface); verify
- `gateway/ai/agent_policy.py` — kept PUBLIC (product surface); verify
- `gateway/ai/collision_detect.py` — kept PUBLIC (product surface); verify
- `gateway/ai/control_plane.py` — kept PUBLIC (product surface); verify
- `gateway/ai/cross_model_audit.py` — kept PUBLIC (product surface); verify
- `gateway/ai/data_plane.py` — kept PUBLIC (product surface); verify
- `gateway/ai/dispatch_gate.py` — kept PUBLIC (product surface); verify
- `gateway/ai/heartbeat.py` — kept PUBLIC (product surface); verify
- `gateway/ai/integrations/opensage_wrapper.py` — kept PUBLIC (product surface); verify
- `gateway/ai/multi_review.py` — kept PUBLIC (product surface); verify
- `gateway/ai/notify.py` — kept PUBLIC (product surface); verify
- `gateway/ai/playbook.py` — kept PUBLIC (product surface); verify
- `gateway/ai/prompt_drift.py` — kept PUBLIC (product surface); verify
- `gateway/ai/seal/HARDENED_SCHEMA.md` — kept PUBLIC (product surface); verify
- `gateway/ai/seal/canon_vectors.json` — kept PUBLIC (product surface); verify
- `gateway/ai/seal/canon_vectors.sha256.json` — kept PUBLIC (product surface); verify
- `gateway/ai/seal/sample_receipt.json` — kept PUBLIC (product surface); verify
- `gateway/ai/secrets_broker.py` — kept PUBLIC (product surface); verify
- `gateway/ai/tenant_auth.py` — kept PUBLIC (product surface); verify
- `gateway/ai/tenant_data.py` — kept PUBLIC (product surface); verify
- `gateway/ai/tenant_paths.py` — kept PUBLIC (product surface); verify
- `gateway/ai/toolcard_cache.py` — kept PUBLIC (product surface); verify
- `gateway/ai/tui.py` — kept PUBLIC (product surface); verify

## FLAGGED — contradicts the directive's exclude list
- `gateway/ai/seal/merkle.py` — the directive listed it as EXCLUDE, but its own
  docstring says *"open-core public layer"* and the PUBLIC verifier
  (`seal/verifier.py`, used by `delimit_seal_verify`) top-level-imports
  `verify_merkle`/`merkle_root` from it. Excluding it BREAKS offline attestation
  verification — a core moat feature. **Classified PUBLIC.** Please confirm.

## PUBLIC — shipped

### MCP server + public support modules
- `gateway/ai/hot_reload.py` (dev hot-reload utility; imported at MCP boot, non-proprietary)
- `gateway/ai/__init__.py`
- `gateway/ai/_compile_status.py`
- `gateway/ai/_paths.py`
- `gateway/ai/activate_helpers.py`
- `gateway/ai/agent_dispatch.py`
- `gateway/ai/agent_policy.py`
### MCP backend bridges (public tool adapters)
- `gateway/ai/backends/__init__.py`
- `gateway/ai/backends/async_utils.py`
- `gateway/ai/backends/deploy_bridge.py`
- `gateway/ai/backends/gateway_core.py`
- `gateway/ai/backends/generate_bridge.py`
- `gateway/ai/backends/git_health.py`
- `gateway/ai/backends/governance_bridge.py`
- `gateway/ai/backends/memory_bridge.py`
- `gateway/ai/backends/ops_bridge.py`
- `gateway/ai/backends/os_bridge.py`
- `gateway/ai/backends/playwright_sandbox.py`
- `gateway/ai/backends/repo_bridge.py`
- `gateway/ai/backends/tools_data.py`
- `gateway/ai/backends/tools_design.py`
- `gateway/ai/backends/tools_infra.py`
- `gateway/ai/backends/tools_real.py`
- `gateway/ai/backends/ui_bridge.py`
- `gateway/ai/backends/vault_bridge.py`
### MCP server + public support modules
- `gateway/ai/checksums.sha256`
- `gateway/ai/cli_contract.py`
- `gateway/ai/collision_detect.py`
- `gateway/ai/context_fs.py`
- `gateway/ai/continuity.py`
- `gateway/ai/control_plane.py`
- `gateway/ai/cross_model_audit.py`
- `gateway/ai/data_plane.py`
### Type stubs for compiled .so engine modules
- `gateway/ai/deliberation.pyi`
### MCP server + public support modules
- `gateway/ai/dispatch_gate.py`
- `gateway/ai/drift_monitor.py`
- `gateway/ai/events.py`
- `gateway/ai/governance.py`
### Type stubs for compiled .so engine modules
- `gateway/ai/governance.pyi`
### MCP server + public support modules
- `gateway/ai/governance_hardening.py`
- `gateway/ai/handoff_preflight.py`
- `gateway/ai/handoff_receipts.py`
- `gateway/ai/heartbeat.py`
- `gateway/ai/integrations/__init__.py`
- `gateway/ai/integrations/opensage_wrapper.py`
- `gateway/ai/key_resolver.py`
- `gateway/ai/last_capture.py`
- `gateway/ai/ledger_manager.py`
- `gateway/ai/ledger_proof.py`
- `gateway/ai/ledger_propose.py`
- `gateway/ai/license.py`
- `gateway/ai/license_consistency.py`
### Type stubs for compiled .so engine modules
- `gateway/ai/license_core.pyi`
### MCP server + public support modules
- `gateway/ai/local_server.py`
- `gateway/ai/mcp_bridge.py`
- `gateway/ai/multi_review.py`
- `gateway/ai/notify.py`
- `gateway/ai/pii_redact.py`
- `gateway/ai/playbook.py`
- `gateway/ai/pro_tools.yaml`
- `gateway/ai/project_config.py`
- `gateway/ai/prompt_drift.py`
- `gateway/ai/rate_limiter.py`
- `gateway/ai/registry_guards.py`
- `gateway/ai/release_sync.py`
- `gateway/ai/remote_resolve.py`
### Public scan surface (delimit_scan)
- `gateway/ai/scan_bridge/__init__.py`
- `gateway/ai/scan_bridge/bridge.py`
- `gateway/ai/scan_bridge/dedup.py`
- `gateway/ai/scan_bridge/digest.py`
- `gateway/ai/scan_bridge/wedge.py`
### MCP server + public support modules
- `gateway/ai/schemas/__init__.py`
### Seal — OPEN-CORE offline attestation verification (customer-facing)
- `gateway/ai/seal/HARDENED_SCHEMA.md`
- `gateway/ai/seal/canon_vectors.json`
- `gateway/ai/seal/canon_vectors.sha256.json`
- `gateway/ai/seal/constitution.json`
- `gateway/ai/seal/merkle.py`
- `gateway/ai/seal/sample_receipt.json`
- `gateway/ai/seal/seal_pubkey.ed25519`
- `gateway/ai/seal/seal_pubkeys.json`
- `gateway/ai/seal/verifier.py`
### MCP server + public support modules
- `gateway/ai/secrets_broker.py`
- `gateway/ai/server.py`
- `gateway/ai/tdqs_lint.py`
- `gateway/ai/tenant_auth.py`
- `gateway/ai/tenant_data.py`
- `gateway/ai/tenant_paths.py`
- `gateway/ai/tool_metadata.py`
- `gateway/ai/toolcard_cache.py`
- `gateway/ai/tracing.py`
- `gateway/ai/tui.py`
- `gateway/ai/usage_allowlist.py`
- `gateway/ai/work_order.py`
### Engine — API governance core (diff/semver/policy/impact/spec-health)
- `gateway/core/__init__.py`
- `gateway/core/auto_baseline.py`
- `gateway/core/ci_formatter.py`
- `gateway/core/complexity_analyzer.py`
- `gateway/core/contract_ledger.py`
- `gateway/core/dependency_graph.py`
- `gateway/core/dependency_manifest.py`
- `gateway/core/diff_engine_v2.py`
- `gateway/core/event_backbone.py`
- `gateway/core/event_schema.py`
- `gateway/core/explainer.py`
- `gateway/core/gateway.py`
- `gateway/core/gateway_v2.py`
- `gateway/core/gateway_v3.py`
- `gateway/core/generator_drift.py`
- `gateway/core/impact_analyzer.py`
- `gateway/core/json_schema_diff.py`
- `gateway/core/openapi_version.py`
- `gateway/core/policies/default.yml`
- `gateway/core/policies/relaxed.yml`
- `gateway/core/policies/strict.yml`
- `gateway/core/policy_engine.py`
- `gateway/core/registry.py`
- `gateway/core/registry_v2.py`
- `gateway/core/registry_v3.py`
- `gateway/core/semver_classifier.py`
- `gateway/core/spec_detector.py`
- `gateway/core/spec_health.py`
- `gateway/core/surface_bridge.py`
- `gateway/core/zero_spec/__init__.py`
- `gateway/core/zero_spec/detector.py`
- `gateway/core/zero_spec/express_extractor.py`
- `gateway/core/zero_spec/fastapi_extractor.py`
- `gateway/core/zero_spec/nestjs_extractor.py`
### Engine — CLI task scripts (validate/diff/policy)
- `gateway/tasks/__init__.py`
- `gateway/tasks/check_policy.py`
- `gateway/tasks/check_policy_v2.py`
- `gateway/tasks/check_policy_v3.py`
- `gateway/tasks/explain_diff.py`
- `gateway/tasks/explain_diff_v2.py`
- `gateway/tasks/validate_api.py`
- `gateway/tasks/validate_api_v2.py`
- `gateway/tasks/validate_api_v3.py`

## INTERNAL — excluded

- `gateway/ai/board_pack.py` -> INTERNAL — board pack builder
- `gateway/ai/board_report.py` -> INTERNAL — autonomous-governance board report
- `gateway/ai/content_engine.py` -> CROSS-VENTURE/MARKETING — YouTube demo + tweet scheduler
- `gateway/ai/content_grounding/__init__.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/build.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/consume.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/features.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/01_missing_evidence.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/02_unknown_evidence_prefix.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/03_banned_comparative.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/04_banned_adoption.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/05_aggregate_no_numeric.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/fail/06_unversioned_inference_rule.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/pass/01_feature_shipped.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/pass/02_aggregate_claim.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/fixtures/pass/03_attestation.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/schemas.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/schemas/asset_provenance.schema.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/schemas/claim.schema.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/schemas/event.schema.json` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_grounding/telemetry.py` -> INTERNAL — content grounding/claim guard (marketing)
- `gateway/ai/content_intel.py` -> INTERNAL — content intelligence (marketing)
- `gateway/ai/corp_dashboard.py` -> INTERNAL — corp status dashboard
- `gateway/ai/custom_gemini_repl.py` -> INTERNAL — internal Gemini REPL plumbing
- `gateway/ai/daemon.py` -> INTERNAL — base daemon runtime
- `gateway/ai/daily_digest.py` -> INTERNAL — autonomous loop daily digest (ops cron)
- `gateway/ai/day90_gate.py` -> INTERNAL — Day-90 gate (internal strategy)
- `gateway/ai/decision_cards.py` -> INTERNAL — board decision cards
- `gateway/ai/deliberation.py` -> PROPRIETARY — multi-model deliberation engine; ships as compiled .so (Vercel), not source
- `gateway/ai/delimit_daemon.py` -> INTERNAL — unified autonomous daemon
- `gateway/ai/dv_mention_tracker.py` -> CROSS-VENTURE — DomainVested mention tracker
- `gateway/ai/evidence_merge_hook.py` -> INTERNAL — evidence merge hook (ops)
- `gateway/ai/focus_gate.py` -> INTERNAL — Jamsons primary-venture focus gate
- `gateway/ai/founding_users.py` -> INTERNAL — founding-users tracker
- `gateway/ai/gemini_vertex_shim.py` -> INTERNAL — internal Vertex model shim
- `gateway/ai/github_scanner.py` -> INTERNAL — GitHub sensing/BD (internal outreach research)
- `gateway/ai/inbox_classifier.py` -> INTERNAL — founder inbox classifier
- `gateway/ai/inbox_daemon.py` -> INTERNAL — founder email control-plane daemon
- `gateway/ai/inbox_daemon_runner.py` -> INTERNAL — founder email control-plane daemon
- `gateway/ai/inbox_drafts/__init__.py` -> INTERNAL — founder inbox draft store
- `gateway/ai/inbox_drafts/registry.py` -> INTERNAL — founder inbox draft store
- `gateway/ai/inbox_drafts/schema.py` -> INTERNAL — founder inbox draft store
- `gateway/ai/inbox_executor.py` -> INTERNAL — founder inbox action executor
- `gateway/ai/led193_daemon/__init__.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/audit.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/cost.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/executor.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/gate.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/pause.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/led193_daemon/picker.py` -> INTERNAL — LED-193 autonomous execution daemon
- `gateway/ai/ledger_deflation.py` -> INTERNAL — board-report P0 deflation sweep
- `gateway/ai/license_core.py` -> PROPRIETARY — license validation core; ships as compiled .so (built at publish), source stripped
- `gateway/ai/loop_daemon.py` -> INTERNAL — autonomous loop daemon
- `gateway/ai/loop_engine.py` -> INTERNAL — autonomous loop engine
- `gateway/ai/outreach_body_gen.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/outreach_gate.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/outreach_instrumentation.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/outreach_loop_daemon.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/outreach_substantive.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/posting_budget.py` -> INTERNAL — social posting budget (marketing)
- `gateway/ai/reaper.py` -> INTERNAL — process reaper (ops)
- `gateway/ai/reddit_proxy.py` -> INTERNAL — Reddit sensing/BD (internal outreach research)
- `gateway/ai/reddit_scanner.py` -> INTERNAL — Reddit sensing/BD (internal outreach research)
- `gateway/ai/report_backlog.py` -> INTERNAL — internal marketing report backlog
- `gateway/ai/reports_distribution.py` -> INTERNAL — Delimit's own marketing distribution engine
- `gateway/ai/reports_distribution_drainer.py` -> INTERNAL — Delimit's own marketing distribution engine
- `gateway/ai/route_daemon.py` -> INTERNAL — notification routing daemon
- `gateway/ai/screen_record.py` -> INTERNAL — screen recording (demo/marketing)
- `gateway/ai/seal/A1_BUNDLE.md` -> INTERNAL — internal design doc
- `gateway/ai/seal/producer.py` -> PROPRIETARY — Seal producer (builds/signs attestations); private key path
- `gateway/ai/seal/verify_a1.py` -> INTERNAL — A1 producer-side helper; no public importer (public verify path is seal/verifier.py)
- `gateway/ai/self_repair/__init__.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/apply.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/cli.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/default_kpis/social_outreach.yaml` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/self_repair/default_self_repair.yaml` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/deliberate.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/diagnose.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/history.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/kpi.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/mode.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/verify.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair/watcher.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/self_repair_daemon.py` -> INTERNAL — self-repair loop (autonomous ops)
- `gateway/ai/sensing/__init__.py` -> INTERNAL — sensing signal store (BD)
- `gateway/ai/sensing/schema.py` -> INTERNAL — sensing signal store (BD)
- `gateway/ai/sensing/signal_store.py` -> INTERNAL — sensing signal store (BD)
- `gateway/ai/sensor_twttr.py` -> CROSS-VENTURE — wire.report X/Twitter budget+sensing
- `gateway/ai/session_phoenix.py` -> INTERNAL — session recovery daemon helper
- `gateway/ai/siem_streaming.py` -> INTERNAL — SIEM streaming (internal security ops)
- `gateway/ai/social.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_archetypes.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_cache.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_capability/__init__.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_capability/capability_validator.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_capability/current_capabilities.yaml` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_capability/fit_floor.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_daemon.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_queue.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/social_target.py` -> INTERNAL — social/marketing automation (Quiet-Attraction internal)
- `gateway/ai/sports_signal_x.py` -> CROSS-VENTURE — wire.report sports signal (MUST never ship)
- `gateway/ai/supabase_sync.py` -> INTERNAL — Supabase infra sync
- `gateway/ai/swarm.py` -> INTERNAL — governed swarm (internal, unmarketed, unarmed)
- `gateway/ai/swarm_infra.py` -> INTERNAL — governed swarm (internal, unmarketed, unarmed)
- `gateway/ai/swarm_marshal/__init__.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/arming.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/catalog.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/changed_files.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/detectors.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/eligibility.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/eligible_scope.json` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/fallback_catalog.json` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/keys.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/marshal.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/marshal_actions.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/nonce_ledger.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/precedence.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/schemas.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_marshal/transforms.py` -> INTERNAL — swarm marshal control plane (arming/keys/nonce/signing)
- `gateway/ai/swarm_p1/__init__.py` -> INTERNAL — swarm P1 autonomous action layer
- `gateway/ai/swarm_p1/actions.py` -> INTERNAL — swarm P1 autonomous action layer
- `gateway/ai/swarm_p1/config.py` -> INTERNAL — swarm P1 autonomous action layer
- `gateway/ai/swarm_p1/daemon.py` -> INTERNAL — swarm P1 autonomous action layer
- `gateway/ai/swarm_safety/__init__.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/claims.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/effects.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/pause.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/policy.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/provenance.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/signing.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/swarm_policy.json` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/swarm_safety/verifier.py` -> INTERNAL — swarm safety/provenance/policy layer
- `gateway/ai/tweet_corpus.py` -> CROSS-VENTURE — wire.report tweet corpus
- `gateway/ai/tweet_corpus_schema.sql` -> CROSS-VENTURE — wire.report tweet corpus
- `gateway/ai/twttr241_budget.py` -> CROSS-VENTURE — wire.report X/Twitter budget+sensing
- `gateway/ai/vendor_news/__init__.py` -> INTERNAL — competitive vendor-news sensing (BD)
- `gateway/ai/vendor_news/drafter.py` -> INTERNAL — competitive vendor-news sensing (BD)
- `gateway/ai/vendor_news/sensor.py` -> INTERNAL — competitive vendor-news sensing (BD)
- `gateway/ai/vendor_news/watchlist.yaml` -> INTERNAL — competitive vendor-news sensing (BD)
- `gateway/ai/workers/__init__.py` -> INTERNAL — autonomous worker pool (outreach/PR drafting, state-change executor)
- `gateway/ai/workers/base.py` -> INTERNAL — autonomous worker pool (outreach/PR drafting, state-change executor)
- `gateway/ai/workers/executor.py` -> INTERNAL — autonomous worker pool (outreach/PR drafting, state-change executor)
- `gateway/ai/workers/outreach_drafter.py` -> INTERNAL — outreach automation (Quiet-Attraction internal)
- `gateway/ai/workers/pr_drafter.py` -> INTERNAL — autonomous worker pool (outreach/PR drafting, state-change executor)
- `gateway/ai/x_ranker.py` -> INTERNAL — X ranking (marketing/sensing)
- `gateway/core/diff_engine_v2.py.bak` -> INTERNAL — internal ops (review)
