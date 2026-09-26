#!/usr/bin/env python3
"""Synthetic governance MCP check using a local delimit-cli tarball.

Usage: scripts/claude-plugin-governance-e2e.py /path/to/delimit-cli-4.21.0.tgz [--keep]
"""
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

helpers = runpy.run_path(str(Path(__file__).with_name("claude-plugin-e2e.py")))
BaseServer = helpers["Server"]
snapshot = helpers["snapshot"]

EXPECTED = {
    "delimit_lint", "delimit_diff", "delimit_semver", "delimit_spec_health",
    "delimit_explain", "delimit_drift_check", "delimit_version", "delimit_help",
}


class Server(BaseServer):
    def __init__(self, env, cwd, err_path):
        # Reuse the records script's JSON-RPC transport with this profile.
        self.p = subprocess.Popen(
            ["delimit", "mcp", "--toolset", "governance"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(err_path, "a"), text=True,
        )
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "delimit-governance-e2e", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})


OLD = """openapi: 3.0.3
info:
  title: Synthetic Orders API
  version: 1.0.0
paths:
  /orders:
    get:
      responses:
        '200':
          description: Orders returned
"""

NEW = """openapi: 3.0.3
info:
  title: Synthetic Orders API
  version: 2.0.0
paths: {}
"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tarball = Path(sys.argv[1]).resolve()
    if not tarball.is_file() or tarball.suffix != ".tgz":
        raise SystemExit("pass a local delimit-cli .tgz tarball")
    keep = "--keep" in sys.argv[2:]
    root = Path(tempfile.mkdtemp(prefix="delimit-governance-e2e-"))
    try:
        home, prefix = root / "home", root / "npm"
        project = home / "projects" / "synthetic-api"
        project.mkdir(parents=True)
        prefix.mkdir()
        old, new = project / "base.yaml", project / "head.yaml"
        old.write_text(OLD)
        new.write_text(NEW)
        env = dict(os.environ, HOME=str(home), npm_config_prefix=str(prefix),
                   npm_config_cache=str(home / ".npm"),
                   PATH=f"{prefix / 'bin'}:{os.environ['PATH']}")
        for key in ("DELIMIT_HOME", "DELIMIT_TOOLSET", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(key, None)
        subprocess.run(["npm", "install", "-g", str(tarball), "--no-fund", "--no-audit"],
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        before = snapshot(home)
        server = Server(env, project, root / "server.stderr")
        try:
            tools = {tool["name"] for tool in server.rpc("tools/list")["tools"]}
            assert tools == EXPECTED, f"tool surface mismatch: {sorted(tools ^ EXPECTED)}"
            diff = server.call("delimit_diff", {"old_spec": str(old), "new_spec": str(new)})
            semver = server.call("delimit_semver", {"old_spec": str(old), "new_spec": str(new)})
        finally:
            server.close()

        breaking = [change for change in diff.get("changes", []) if change.get("is_breaking")]
        assert diff.get("breaking_changes", 0) >= 1, f"no breaking change count: {diff}"
        assert any(change.get("type") == "endpoint_removed" and "/orders" in change.get("path", "")
                   for change in breaking), f"removed endpoint absent: {diff}"
        assert semver.get("bump") == "MAJOR", f"expected MAJOR bump: {semver}"

        written = snapshot(home) - before
        outside = sorted(p for p in written if not p.startswith((".delimit/", ".npm/")))
        assert not outside, f"files written outside ~/.delimit and npm cache: {outside[:10]}"
        print(json.dumps({"ok": True, "tools": len(tools), "breaking_changes": len(breaking),
                          "bump": semver["bump"], "home": str(home)}, indent=2))
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
