#!/usr/bin/env python3
"""Synthetic MCP check for a local delimit-cli tarball, without model calls.

Usage: scripts/claude-plugin-panel-e2e.py /absolute/path/delimit-cli-4.21.0.tgz [--keep]
Installation may use npm/pip package caches or registries. MCP checks themselves
use an isolated HOME with no model credentials, hosted keys, or cloud sync.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

EXPECTED = {
    "delimit_deliberate", "delimit_deliberation_status", "delimit_models",
    "delimit_version", "delimit_help",
}


class Server:
    def __init__(self, env, cwd, stderr_path):
        self.stderr = stderr_path.open("w")
        self.process = subprocess.Popen(
            ["delimit", "mcp", "--toolset", "panel"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True,
        )
        self.sequence = 0
        self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "delimit-panel-e2e", "version": "1"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "id": self.sequence, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError(f"MCP server exited during {method}")
            result = json.loads(line)  # Reject any non-JSON stdout.
            if result.get("id") == self.sequence:
                assert "error" not in result, (method, result.get("error"))
                return result["result"]

    def call(self, name):
        result = self.rpc("tools/call", {"name": name, "arguments": {}})
        assert not result.get("isError"), (name, result)
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        content = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return json.loads(content)

    def close(self):
        self.process.stdin.close()
        self.process.terminate()
        self.process.wait(timeout=20)
        self.stderr.close()


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tarball = Path(sys.argv[1]).resolve()
    assert tarball.is_file() and tarball.suffix == ".tgz", f"local .tgz required: {tarball}"
    with tarfile.open(tarball, "r:gz") as archive:
        manifest = json.load(archive.extractfile("package/package.json"))
    assert manifest.get("name") == "delimit-cli" and manifest.get("version") == "4.21.0", manifest
    keep = "--keep" in sys.argv[2:]
    root = Path(tempfile.mkdtemp(prefix="delimit-panel-e2e-"))
    home, prefix, project = root / "home", root / "npm", root / "project"
    for directory in (home, prefix, project, home / ".delimit"):
        directory.mkdir(parents=True, exist_ok=True)
    # An explicit empty config prevents CLI discovery and hosted fallback.
    (home / ".delimit" / "models.json").write_text("{}\n")
    env = dict(os.environ)
    for key in list(env):
        if key.endswith("_API_KEY") or key.startswith(("DELIMIT_", "SUPABASE_", "GOOGLE_", "GCLOUD_")):
            env.pop(key, None)
    env.update(HOME=str(home), npm_config_prefix=str(prefix),
               npm_config_cache=str(home / ".npm"),
               PATH=f"{prefix / 'bin'}:{os.environ['PATH']}",
               DELIMIT_DISABLE_CLOUD_SYNC="1", DELIMIT_NON_INTERACTIVE="1")
    server = None
    try:
        subprocess.run(["npm", "install", "-g", str(tarball), "--no-fund", "--no-audit"],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        server = Server(env, project, root / "server.stderr")
        tools = {tool["name"] for tool in server.rpc("tools/list")["tools"]}
        assert tools == EXPECTED, f"panel tools differ: {sorted(tools ^ EXPECTED)}"
        models = server.call("delimit_models")
        # Delimit may gate model management by license. Both a clear gate and
        # an empty model inventory are sane for a clean Free installation.
        if "error" in models:
            assert models.get("status") == "premium_required", models
        else:
            assert not models.get("configured_models"), models
        status = server.call("delimit_deliberation_status")
        assert status.get("mode") == "none", status
        assert status.get("oauth_signed_in") is False, status
        assert "No models available" in status.get("note", ""), status
        print(json.dumps({"ok": True, "tools": sorted(tools), "mode": status["mode"],
                          "models_result": "license_gate" if "error" in models else "empty",
                          "home": str(home)}, indent=2))
    finally:
        if server is not None:
            server.close()
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
