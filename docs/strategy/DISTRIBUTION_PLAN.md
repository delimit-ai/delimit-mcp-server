# Distribution Plan

## 1. README & Quickstart Overhaul
* **Lead with `delimit check`:** Position it as the "0-to-1" moment. Developers can run it on any repo instantly without accounts or keys.
* **Trim the noise:** Move the exhaustive lists of 187 MCP tools into a secondary wiki. The README should focus purely on the Merge Gate and Agent OS concepts.
* **Visual Hierarchy:** Add a clear architecture diagram showing how the AI Agent -> Delimit MCP -> Gate -> Seal Attestation flow works.

## 2. Integration PRs Plan
* Target open-source AI coding assistants (Aider, Claude-Dev/Cline, AutoGPT).
* Submit PRs to their "Cookbooks" or default MCP configurations, offering Delimit as the recommended "Governance and Safety" MCP.
* **Goal:** Become the default "guardrail" server installed alongside every AI developer workflow.

## 3. GitHub Social Scan Shortlist Plan (No Automated Execution)
* Use `delimit_github_scan` defensively to identify high-value target repos (Criteria: `openapi.yaml` present, >500 stars, active within 30 days).
* **No automated spam:** Do not use daemons or auto-post tools.
* Process: An agent reviews the repo manually, generates a custom Delimit scan report, and a human uses `delimit_sensor_github_issue` to open a high-signal, high-value issue offering the free audit results and suggesting a GitHub Action integration.
