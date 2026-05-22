"""
Feature whitelist builder for the content grounding layer (LED-1084 Week 2).

Builds `~/.delimit/content/grounding/features.json` from two sources of truth:
  1. MCP tool registry  — every `@mcp.tool()` decorator in ai/server.py is
                           a shipped capability the drafter may reference.
  2. CLI subcommands    — parsed from `delimit --help` output (bin/delimit-cli.js
                           `program.command(...)` entries) when the CLI binary
                           is on PATH. Best-effort; we fall back to just the
                           MCP tool set if the CLI parse fails.

The grounding gate in ai/content_grounding/consume.py calls
`load_feature_whitelist()` which reads this file. An empty whitelist
causes the gate to flag ANY feature-claim language, so populating this
file is a direct prerequisite for meaningful gate scores.

Running this builder is idempotent and safe — it overwrites `features.json`
with the fresh set, keeps the schema stable, and records `built_at` +
`source_counts` for audit.

Usage:
    python -m ai.content_grounding.features build
    # or programmatically:
    from ai.content_grounding.features import build_and_persist_features
    build_and_persist_features()
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .consume import FEATURES_FILE

logger = logging.getLogger("delimit.ai.content_grounding.features")

GATEWAY_ROOT = Path(os.environ.get("DELIMIT_GATEWAY_REPO", "/home/delimit/delimit-gateway"))
NPM_BIN = Path(os.environ.get("DELIMIT_CLI_BIN", "/home/delimit/npm-delimit/bin/delimit-cli.js"))


# ---------------------------------------------------------------------------
# Source 1 — MCP tool registry (ai/server.py)
# ---------------------------------------------------------------------------

_MCP_TOOL_DEF_RE = re.compile(
    r"@mcp\.tool\(\)\s*\n\s*def\s+(delimit_[a-z0-9_]+)\s*\(",
    re.MULTILINE,
)


def extract_mcp_tools(server_py: Path) -> List[str]:
    """Extract every `delimit_<name>` tool registered in server.py.

    Returns sorted unique tool names. These are the shipped MCP tools the
    drafter is allowed to name. A draft that mentions `delimit_wrap` or
    `delimit_trust_page` passes the feature gate; one that mentions
    `delimit_coinbase_integration` fails.
    """
    if not server_py.is_file():
        logger.warning("MCP server source not found: %s", server_py)
        return []
    try:
        text = server_py.read_text(errors="replace")
    except Exception as e:
        logger.warning("could not read MCP server source: %s", e)
        return []
    tools = sorted(set(_MCP_TOOL_DEF_RE.findall(text)))
    return tools


# ---------------------------------------------------------------------------
# Source 2 — CLI subcommands (bin/delimit-cli.js)
# ---------------------------------------------------------------------------

# Commander.js patterns we grep for:
#   program.command("foo")
#   program.command('foo [bar]')
#   .command("foo <arg>")
# Captures just the subcommand name (first token up to space).
_CLI_COMMAND_RE = re.compile(
    r"""(?:^|\.)command\(\s*['"]([a-z][a-z0-9_-]*)""",
    re.MULTILINE | re.IGNORECASE,
)


def extract_cli_commands(cli_path: Path) -> List[str]:
    """Extract CLI subcommand names from bin/delimit-cli.js.

    Works statically (no subprocess), so it's safe on CI runners without
    node installed. Returns sorted unique command names.
    """
    if not cli_path.is_file():
        logger.warning("delimit CLI source not found: %s", cli_path)
        return []
    try:
        text = cli_path.read_text(errors="replace")
    except Exception as e:
        logger.warning("could not read CLI source: %s", e)
        return []
    commands = set()
    for m in _CLI_COMMAND_RE.finditer(text):
        name = m.group(1).strip().lower()
        # Filter out obvious false matches (method names that happen to start
        # with a word followed by '('). A real commander subcommand won't
        # contain certain tokens.
        if name in {"log", "error", "warn", "info", "on", "off", "then", "catch", "parse"}:
            continue
        commands.add(name)
    return sorted(commands)


# ---------------------------------------------------------------------------
# Build + persist
# ---------------------------------------------------------------------------

def build_feature_set(
    gateway_root: Optional[Path] = None,
    cli_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Aggregate features from both sources into a dict ready to persist.

    Shape:
      {
        "version": 1,
        "built_at": "2026-04-24T...",
        "features": ["delimit_lint", "delimit_wrap", "init", "scan", ...],
        "source_counts": {"mcp_tools": N, "cli_commands": M},
        "sources": {
          "mcp_tools": [...],
          "cli_commands": [...],
        }
      }
    """
    gw = gateway_root or GATEWAY_ROOT
    cp = cli_path or NPM_BIN
    mcp_tools = extract_mcp_tools(gw / "ai" / "server.py")
    cli_commands = extract_cli_commands(cp)
    features = sorted(set(mcp_tools) | set(cli_commands))
    return {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "features": features,
        "source_counts": {
            "mcp_tools": len(mcp_tools),
            "cli_commands": len(cli_commands),
            "total_unique": len(features),
        },
        "sources": {
            "mcp_tools": mcp_tools,
            "cli_commands": cli_commands,
        },
    }


def build_and_persist_features(
    out_path: Optional[Path] = None,
    gateway_root: Optional[Path] = None,
    cli_path: Optional[Path] = None,
) -> Path:
    """Build the feature set and write it to features.json.

    Returns the path written. Overwrites any previous file.
    """
    target = Path(out_path) if out_path else FEATURES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    data = build_feature_set(gateway_root=gateway_root, cli_path=cli_path)
    target.write_text(json.dumps(data, indent=2))
    logger.info(
        "wrote features.json: %d mcp_tools + %d cli_commands → %d unique → %s",
        data["source_counts"]["mcp_tools"],
        data["source_counts"]["cli_commands"],
        data["source_counts"]["total_unique"],
        target,
    )
    return target


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("build", "show"):
        print(
            "usage: python -m ai.content_grounding.features build | show\n"
            "  build  rebuild features.json from MCP + CLI sources\n"
            "  show   print current features.json stats",
            file=sys.stderr,
        )
        return 2
    if sys.argv[1] == "build":
        out = build_and_persist_features()
        data = json.loads(out.read_text())
        print(f"wrote {out}")
        print(f"  mcp_tools:      {data['source_counts']['mcp_tools']}")
        print(f"  cli_commands:   {data['source_counts']['cli_commands']}")
        print(f"  total unique:   {data['source_counts']['total_unique']}")
        return 0
    if sys.argv[1] == "show":
        if not FEATURES_FILE.exists():
            print(f"no features.json at {FEATURES_FILE}. run 'build' first.", file=sys.stderr)
            return 1
        data = json.loads(FEATURES_FILE.read_text())
        print(f"features.json  built_at={data.get('built_at')}")
        print(f"  total:  {data['source_counts']['total_unique']}")
        print(f"  sample: {data['features'][:8]} ...")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_main())
