# Data handling

Delimit Governance starts a local MCP server over stdio. Its checks read the spec files you point them at. `api-check` also uses a temporary copy of the base branch spec and removes it afterward. The governance tools do not send telemetry or store your spec contents. Local server files, its Python environment, and operational logs can be stored under `~/.delimit`.

On first launch, `npx` may download `delimit-cli` from npm and `pip` may download Python dependencies from PyPI. If you pass an HTTP(S) spec URL to `delimit_lint`, that URL is fetched into a temporary file for the check. Local path inputs do not trigger a spec fetch. Claude Code processes any results you choose to show it under its own terms.

Uninstalling the plugin leaves `~/.delimit` in place. Inspect and remove that directory yourself if you want to delete its local files.
