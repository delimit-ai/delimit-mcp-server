# Muse: direct, scoped Delimit MCP

`scripts/muse_mcp.py` connects a Muse MCP client directly to an existing Delimit
stdio server. It uses the Python MCP SDK already installed with Delimit, not a
model API, alternate ledger, background daemon, or new HTTP service.

The connector exposes **only `delimit_ledger_context`**, with no client arguments.
The operator fixes the venture on its command line. It rejects every other tool
and never forwards resource, prompt, sampling, or execution requests. Backend
errors/timeouts consume the read allowance; oversized output is rejected rather
than silently truncated. Backend error results keep `isError: true` but replace
their potentially sensitive contents with a generic error.
Evidence on stderr contains only a result hash, byte count, and error flag.

## Isolated synthetic verification first

From a source checkout, run the stdio integration and negative tests using
Delimit's existing Python (the npm tarball does not include the test fixture):

```sh
python -m unittest discover -s tests -p test_muse_mcp.py -v
```

No Muse inference, private ledger, credentials, billing, or external posting is
needed by these tests. Passing them establishes MCP protocol/scope behavior, not
Muse model quality, paid-account use, or unattended execution.

## Conditional Muse configuration

Use the installed Muse settings path in an **isolated evaluation config root**.
Do not replace a user's global settings. Muse Code 1.1.1's settings-based MCP
entry uses a string `command` and an `args` array (unlike plugin manifests).
Replace the example paths with operator-verified absolute paths:

```json
{
  "mcpServers": {
    "delimit_scoped": {
      "enabled": true,
      "transport": "stdio",
      "command": "/absolute/path/to/delimit-python",
      "args": [
        "/absolute/path/to/scripts/muse_mcp.py",
        "--venture", "synthetic-demo",
        "--max-calls", "1",
        "--", "/absolute/path/to/delimit-python",
        "/absolute/path/to/tests/fixtures/muse_mcp_backend.py",
        "normal", "/absolute/path/to/disposable/synthetic-audit.jsonl"
      ]
    }
  }
}
```

Use an interpreter whose environment already contains the MCP SDK for both
processes. The SDK starts the backend with a restricted default environment;
do not depend on `PYTHONPATH` or arbitrary parent variables propagating to it.
The synthetic audit directory must exist and be writable.

For a later data-authorized real connection, the backend argv points to the
existing Delimit server and the venture is explicitly selected by the operator.
Do not attach private context simply because the connector works. Verify the
chosen model's data-use route and subscription binding separately. Keep tokens
out of settings examples, prompts, command arguments, receipts and commits.

## Deliberate limitations

- **This does not bypass Muse approvals.** Tested Muse 1.1.1 `exec` loads MCP but
  can wait for approval; the tested MSP `serve` path did not load its configured
  MCP tools. A native approval may still be required. Do not claim that a stdio
  test fixes this headless-client limitation or change to `never`/`--yolo`.
- Default allowance is one read per connector process; `--max-calls` permits
  1–10 explicitly chosen reads. This is not a persistent cross-restart permit,
  exactly-once execution system, token budget, or a paid-usage meter.
- The backend is trusted code selected by the operator. The connector does not
  sandbox its filesystem, credentials, startup side effects, or implementation
  of venture scoping. It enforces the forwarded request, not a multi-tenant data
  isolation guarantee. Test with a synthetic backend before any real attachment.
- No production routing or global client configuration is installed by this
  script. Disable the connection by removing only its evaluation MCP entry and
  terminating its owning client; preserve other harness settings and evidence.
