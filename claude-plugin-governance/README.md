# Delimit Governance for Claude Code

Check OpenAPI changes before a PR. The plugin exposes local lint, diff, semver, spec health, explanation, and drift tools, with short skills for API checks, quality review, and change notes. The same checks can run in CI with [delimit-ai/delimit-action](https://github.com/delimit-ai/delimit-action).

## Requirements

- Node.js 18 or later and `npx`
- Python 3.9 or later with `venv` and `pip`

## Install

After this plugin is listed in the Delimit marketplace, install it in Claude Code with `claude plugin install delimit-governance@delimit`. Its first launch uses `npx` to fetch the pinned `delimit-cli@4.21.0` package from npm and may install Python dependencies from PyPI. The local MCP server runs over stdio; it does not run `delimit setup`.

## Local data and network use

The tools read the OpenAPI files you name. `api-check` writes a temporary copy of the base branch spec and removes it afterward. The launcher stores its server copy and Python environment under `~/.delimit/plugin-server/<version>`; local operational logs can also be written under `~/.delimit`. The governance tools do not store your spec contents or send telemetry. Apart from installation, a spec URL passed to `delimit_lint` is fetched; local file checks need no network. See [PRIVACY.md](PRIVACY.md).

## Support

pro@delimit.ai · https://github.com/delimit-ai/delimit-mcp-server/issues
