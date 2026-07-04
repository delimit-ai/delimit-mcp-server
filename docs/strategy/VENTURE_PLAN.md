# Delimit Venture Plan & Strategy

## 1. Inventory Summary
* **MCP Tool Registry:** Currently massive at ~187 tools spanning governance, memory, OS, design, testing, and deployment.
* **Detection Engine:** 28 total change types detected deterministically (17 breaking, 11 non-breaking).
* **Core Features:** 
  * *Ledger:* Persistent task tracking shared across sessions and different AI assistants.
  * *Vault & BYOK:* AES-256 encrypted local `secret_store` for API keys; BYOK enables multi-model deliberation (GPT-4o, Claude, Gemini, Grok) without SaaS lock-in.
* **Release Cadence:** Extremely high velocity (9 releases between v4.9.0 on June 15 and v4.14.2 on July 4, 2026).
* **Onboarding Path:** Streamlined via `delimit scan` (discovery) -> `delimit init` (wiring) -> `delimit check` (zero-config PR safety gate). Docs at delimit.ai/docs.
* **Project Metrics:** Early stage; ~19 GitHub stars, 0 open PRs, predominantly single/core-maintainer driven.

## 2. Competitive Positioning
* **vs. oasdiff:** oasdiff is the gold standard for pure OpenAPI diffing. Delimit goes further by wrapping the diff in a *policy engine*, adding CI/CD gates, and providing multi-model AI deliberation to resolve subjective bumps.
* **vs. Optic:** Optic focuses heavily on API governance but relies on a SaaS model. Delimit's local-first MCP architecture embeds governance directly into the AI agent's workflow.
* **vs. Buf:** Buf dominates Protobuf; Delimit claims the OpenAPI/Swagger territory.
* **vs. GitHub's MCP Direction:** As GitHub builds native MCPs for issues/PRs, Delimit must remain cross-platform (Cursor, Aider, Claude Code, Gemini CLI) and focus on cryptographic attestation (Delimit Seal) that outlives a single platform.

## 3. The Defensible Wedge
**The Attested Merge Gate for AI Code.** 
Delimit's wedge is not just linting; it's the cryptographic "Seal" receipt. As AI generates more code, teams need verifiable proof that code passed security, test, and API compatibility gates before merge. Delimit provides an offline-verifiable attestation of exactly what the AI did and how it was checked.
