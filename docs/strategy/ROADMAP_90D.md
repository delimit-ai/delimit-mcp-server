# 90-Day Feature Roadmap

### 1. First-Class `delimit handoff` CLI
* **Spec:** A dedicated CLI command that serializes session state, uncommitted git diffs, and ledger context into a compressed `.delimit/handoff.bundle`. Enables seamless switching between Cursor, Claude Code, and Gemini.
* **Effort:** Medium (2 weeks).
* **Value:** Cements Delimit as the definitive cross-model Agent OS.

### 2. OpenAPI 3.1 Coverage Gaps vs oasdiff
* **Spec:** Upgrade the deterministic diff engine to fully support OpenAPI 3.1 features (e.g., webhooks, full JSON Schema compliance).
* **Effort:** High (4 weeks).
* **Value:** Essential to match the industry gold-standard (oasdiff) and prevent false positives in enterprise usage.

### 3. Policy-as-Code Governance Config
* **Spec:** Transition from basic YAML rule arrays to a lightweight embedded Rego or CUE execution engine.
* **Effort:** Medium (3 weeks).
* **Value:** Unlocks complex, conditional enterprise API policies (e.g., "if header X is present, field Y must be removed").

### 4. SARIF Output for GitHub Code Scanning
* **Spec:** Add `--format sarif` to `delimit check` and `delimit lint`.
* **Effort:** Low (1 week).
* **Value:** Quick win. Seamlessly surfaces Delimit violations directly inside the GitHub Advanced Security UI.

### 5. Published Dogfood Case Study
* **Spec:** A comprehensive technical blog post analyzing how Delimit uses Delimit to govern its own API and MCP tool surface (delimit-mcp-server self-attestation).
* **Effort:** Low (1 week).
* **Value:** Crucial for GTM. Proves the value of the cryptographic "Seal" receipt in production.

*(Note on Opt-in Usage Telemetry: Evaluated but deprioritized. While highly valuable for maintainers, it introduces privacy friction for local-first users. We will rely on npm/GitHub download metrics and community feedback for the next 90 days.)*
