#!/usr/bin/env python3
"""End-to-end check for the Delimit Claude plugin's records workflow.

Installs delimit-cli (a local .tgz or a registry spec) into a throwaway HOME and
npm prefix, launches `delimit mcp --toolset records` TWICE from a synthetic
project directory, and drives the plugin's workflow over MCP stdio:

  record a decision -> write a handoff -> resume (ledger_context + handoff_list)

It asserts: exactly the 10 records tools are exposed; stdout carries only
JSON-RPC; the second launch works (reused venv); the records land under
$HOME/.delimit; nothing else in HOME is written except npm's own cache.
Synthetic data only. Uses no real user configuration.

Usage: scripts/claude-plugin-e2e.py <delimit-cli spec or path to .tgz> [--keep]
       scripts/claude-plugin-e2e.py --installed [--keep]  # use installed CLI
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED = {
    "delimit_ledger_add", "delimit_ledger_list", "delimit_ledger_update",
    "delimit_ledger_done", "delimit_ledger_context", "delimit_handoff_create",
    "delimit_handoff_list", "delimit_handoff_acknowledge", "delimit_version",
    "delimit_help",
}


class Server:
    def __init__(self, env, cwd, err_path, toolset="records"):
        self.p = subprocess.Popen(["delimit", "mcp", "--toolset", toolset], cwd=cwd, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=open(err_path, "a"), text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "delimit-plugin-e2e", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise SystemExit(f"server exited during {method}")
            data = json.loads(line)  # anything but JSON-RPC on stdout fails here
            if data.get("id") == self.n:
                if "error" in data:
                    raise SystemExit(f"{method} error: {data['error']}")
                return data["result"]

    def call(self, name, args):
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise SystemExit(f"{name} failed: {text[:300]}")
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self):
        self.p.stdin.close()
        self.p.terminate()
        self.p.wait(timeout=20)


def snapshot(home: Path):
    return {str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    spec, keep = sys.argv[1], "--keep" in sys.argv
    installed = spec == "--installed"
    root = Path(tempfile.mkdtemp(prefix="delimit-plugin-e2e-"))
    home, prefix, project = root / "home", root / "npm", root / "home" / "projects" / "orders-service"
    for d in (home, prefix, project):
        d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), npm_config_prefix=str(prefix),
               npm_config_cache=str(root / "npm-cache"),
               PATH=f"{prefix / 'bin'}:{os.environ['PATH']}")
    env.pop("DELIMIT_HOME", None)
    env.pop("DELIMIT_TOOLSET", None)
    if not installed:
        subprocess.run(["npm", "install", "-g", spec, "--no-fund", "--no-audit"], env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    before = snapshot(home)
    err = root / "server.stderr"

    s = Server(env, project, err)
    tools = {t["name"] for t in s.rpc("tools/list")["tools"]}
    assert tools == EXPECTED, f"tool surface mismatch: {sorted(tools ^ EXPECTED)}"
    added = s.call("delimit_ledger_add", {
        "title": "Use Postgres for the orders service (synthetic)",
        "description": "Chosen over SQLite for concurrent writes.", "priority": "P1"})
    item_id = added["added"]["id"]
    s.call("delimit_handoff_create", {
        "task_description": "Orders service persistence (synthetic)",
        "completed": "Schema drafted; 12/12 unit tests passed (synthetic)",
        "not_completed": "Migration 002 not written",
        "next_action": "Write migration 002 and run the integration tests"})
    s.close()

    s = Server(env, project, err)  # second launch: reused venv, same project
    ctx = s.call("delimit_ledger_context", {})
    handoffs = s.call("delimit_handoff_list", {})
    s.close()
    assert any(i.get("id") == item_id for i in ctx.get("next_up", [])), f"decision not resumed: {ctx}"
    assert handoffs.get("count", 0) >= 1, f"handoff not resumed: {handoffs}"

    full = Server(env, project, err, toolset="full")
    full_tools = {t["name"] for t in full.rpc("tools/list")["tools"]}
    assert {"delimit_ledger_context", "delimit_handoff_list"} <= full_tools, "full toolset lacks records reads"
    full_ctx = full.call("delimit_ledger_context", {})
    full_handoffs = full.call("delimit_handoff_list", {})
    full.close()
    assert any(i.get("id") == item_id for i in full_ctx.get("next_up", [])), f"full toolset missed decision: {full_ctx}"
    assert full_handoffs.get("count", 0) >= 1, f"full toolset missed handoff: {full_handoffs}"

    written = snapshot(home) - before
    outside = sorted(p for p in written if not p.startswith((".delimit/", ".npm/")))
    assert not outside, f"files written outside ~/.delimit: {outside[:10]}"
    print(json.dumps({"ok": True, "tools": len(tools), "decision": item_id,
                      "handoffs": handoffs.get("count"), "full_toolset_readback": True, "files_under_delimit":
                      len([p for p in written if p.startswith('.delimit/')]),
                      "home": str(home)}, indent=1))
    if not keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
