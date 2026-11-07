# Privacy Policy for Delimit Lite

Delimit Lite is committed to protecting your privacy and developer autonomy.

## Data Processing and Architecture

- **No Remote Telemetry**: Delimit Lite contains no background daemons, network clients, tracking analytics, or telemetry probes.
- **Static Artifacts Only**: The plugin distributes static Markdown skill definitions and configuration files. It contains no executable scripts, binaries, or hooks.
- **Local CLI Execution**: All commands suggested by the skills are executed locally on your machine via the `delimit-cli` command line utility only under your explicit permission in your terminal session. Every skill runs the CLI only when you explicitly ask (e.g. "resume", "what was I doing", "record this"); no commands run automatically at session start or in the background.
- **Local Context Storage**: Session records, handoffs, and project memories managed by Delimit remain strictly on your local filesystem under your project repository and `~/.delimit` directory. No project source code, file contents, or conversation transcripts are uploaded to remote servers.

## Open Source Integrity

This plugin is open source under the MIT License and developed by Delimit AI.
For questions or security concerns, visit https://delimit.ai or open an issue at https://github.com/delimit-ai/delimit-mcp-server/issues.
