"""Independent scoped-adapter tests; all data and backends are synthetic."""

import asyncio
import contextlib
import importlib.util
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "muse_mcp.py"
FIXTURE = ROOT / "tests" / "fixtures" / "muse_mcp_backend.py"
spec = importlib.util.spec_from_file_location("muse_mcp_under_test", MODULE)
muse_mcp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = muse_mcp
spec.loader.exec_module(muse_mcp)


def result(text="synthetic result", **kwargs):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], **kwargs
    )


class Backend:
    def __init__(self, output=None, error=None, delay=0):
        self.output = output if output is not None else result()
        self.error = error
        self.delay = delay
        self.calls = []
        self.active = 0
        self.peak_active = 0

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.active += 1
        self.peak_active = max(self.active, self.peak_active)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.output
        finally:
            self.active -= 1


class AdapterUnitTests(unittest.IsolatedAsyncioTestCase):
    async def make_server(self, backend=None, **kwargs):
        self.backend = backend or Backend()
        server = muse_mcp.build_server(self.backend, "synthetic-demo", **kwargs)
        return await server if inspect.isawaitable(server) else server

    async def call(self, server, name="delimit_ledger_context", arguments=None):
        request = types.CallToolRequest(params=types.CallToolRequestParams(
            name=name, arguments={} if arguments is None else arguments
        ))
        return (await server.request_handlers[types.CallToolRequest](request)).root

    async def test_only_scoped_read_tool_is_advertised(self):
        server = await self.make_server()
        listed = (await server.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest()
        )).root.tools
        self.assertEqual([tool.name for tool in listed], ["delimit_ledger_context"])
        self.assertEqual(listed[0].inputSchema.get("properties", {}), {})
        self.assertIs(listed[0].inputSchema.get("additionalProperties"), False)
        self.assertTrue(listed[0].annotations.readOnlyHint)

    async def test_fixed_venture_replaces_frontend_authority(self):
        server = await self.make_server()
        output = await self.call(server)
        self.assertFalse(output.isError)
        self.assertEqual(self.backend.calls, [
            ("delimit_ledger_context", {"venture": "synthetic-demo"})
        ])

    async def test_caller_cannot_select_venture_or_other_arguments(self):
        server = await self.make_server()
        for arguments in ({"venture": "production"}, {"limit": 999}, {"x": {}}):
            with self.subTest(arguments=arguments):
                self.assertTrue((await self.call(server, arguments=arguments)).isError)
        self.assertEqual(self.backend.calls, [])

    async def test_unknown_write_and_namespaced_tools_are_rejected(self):
        server = await self.make_server()
        for name in ("forbidden_write_tool", "delimit_ledger_add", "mcp__delimit_ledger_context", ""):
            with self.subTest(name=name):
                self.assertTrue((await self.call(server, name=name)).isError)
        self.assertEqual(self.backend.calls, [])

    async def test_default_budget_is_one(self):
        server = await self.make_server()
        self.assertFalse((await self.call(server)).isError)
        self.assertTrue((await self.call(server)).isError)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_backend_exception_is_sanitized_and_consumes_budget(self):
        server = await self.make_server(Backend(error=RuntimeError("PRIVATE_SENTINEL token=secret")))
        output = await self.call(server)
        self.assertTrue(output.isError)
        self.assertNotIn("PRIVATE_SENTINEL", output.model_dump_json())
        self.assertNotIn("token=secret", output.model_dump_json())
        self.assertTrue((await self.call(server)).isError)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_backend_error_result_does_not_leak_raw_error(self):
        server = await self.make_server(Backend(output=result("PRIVATE_SENTINEL", isError=True)))
        output = await self.call(server)
        self.assertTrue(output.isError)
        self.assertNotIn("PRIVATE_SENTINEL", output.model_dump_json())

    async def test_serialized_result_bound_counts_structured_content(self):
        server = await self.make_server(Backend(output=result(
            "small", structuredContent={"payload": "x" * 33000}
        )))
        output = await self.call(server)
        self.assertTrue(output.isError)
        self.assertLess(len(output.model_dump_json().encode()), 32768)

    async def test_result_bound_counts_utf8_bytes_not_characters(self):
        server = await self.make_server(Backend(output=result("界" * 12000)))
        self.assertTrue((await self.call(server)).isError)

    async def test_success_preserves_structured_synthetic_evidence(self):
        expected = result("fixture", structuredContent={"synthetic": True, "nonce": "n1"})
        server = await self.make_server(Backend(output=expected))
        self.assertEqual((await self.call(server)).model_dump(), expected.model_dump())

    async def test_concurrent_calls_serialize_and_do_not_overspend(self):
        server = await self.make_server(Backend(delay=0.02), max_calls=2)
        outcomes = await asyncio.gather(*(self.call(server) for _ in range(5)))
        self.assertEqual(sum(not item.isError for item in outcomes), 2)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.backend.peak_active, 1)

    async def test_timeout_is_bounded_sanitized_and_consumes_budget(self):
        server = await self.make_server(Backend(delay=5), timeout_seconds=1)
        output = await asyncio.wait_for(self.call(server), timeout=2)
        self.assertTrue(output.isError)
        self.assertTrue((await self.call(server)).isError)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.active, 0)

    async def test_no_resource_or_prompt_passthrough_handlers(self):
        server = await self.make_server()
        for request in (types.ListResourcesRequest, types.ReadResourceRequest,
                        types.ListResourceTemplatesRequest, types.ListPromptsRequest,
                        types.GetPromptRequest):
            self.assertNotIn(request, server.request_handlers)


class AdapterStdioTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.asynccontextmanager
    async def session(self, mode="ok"):
        with tempfile.TemporaryDirectory(prefix="muse-mcp-test-") as directory:
            audit = Path(directory) / "audit.jsonl"
            params = StdioServerParameters(command=sys.executable, args=[
                str(MODULE), "--venture", "synthetic-demo", "--timeout-seconds", "2",
                "--max-calls", "1", "--", sys.executable, str(FIXTURE), mode, str(audit)
            ])
            with open(Path(directory) / "stderr.log", "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        yield client, audit

    async def test_real_stdio_handshake_list_and_fixed_scope_call(self):
        async with self.session() as (client, audit):
            listed = await client.list_tools()
            self.assertEqual([tool.name for tool in listed.tools], ["delimit_ledger_context"])
            denied = await client.call_tool("forbidden_write_tool", {"venture": "root"})
            self.assertTrue(denied.isError)
            denied = await client.call_tool("delimit_ledger_context", {"venture": "root"})
            self.assertTrue(denied.isError)
            actual = await client.call_tool("delimit_ledger_context", {})
            self.assertFalse(actual.isError)
            evidence = actual.structuredContent or json.loads(actual.content[0].text)
            self.assertEqual(evidence["venture"], "synthetic-demo")
            self.assertTrue(evidence["synthetic"])
            self.assertEqual(evidence["open_item"], "SYN-E")
            self.assertTrue((await client.call_tool("delimit_ledger_context", {})).isError)
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual([e for e in events if e["event"] == "called"], [
                {"event": "called", "venture": "synthetic-demo"}
            ])
            self.assertFalse(any(e["event"] == "forbidden_called" for e in events))

    async def test_real_stdio_backend_error_is_sanitized(self):
        async with self.session("error") as (client, _):
            output = await client.call_tool("delimit_ledger_context", {})
            self.assertTrue(output.isError)
            self.assertNotIn("BACKEND_PRIVATE_ERROR_SENTINEL", output.model_dump_json())


class AdapterCLITests(unittest.TestCase):
    def test_cli_rejects_invalid_caps_before_backend_start(self):
        for flag, value in (("--max-calls", "0"), ("--max-calls", "11"),
                            ("--timeout-seconds", "0"), ("--timeout-seconds", "61")):
            with self.subTest(flag=flag, value=value):
                process = subprocess.run([sys.executable, str(MODULE), "--venture", "synthetic-demo",
                    flag, value, "--", "must-not-execute-nonexistent-backend"],
                    capture_output=True, text=True, timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertNotIn("FileNotFoundError", process.stderr)

    def test_missing_backend_tool_fails_startup_without_invocation(self):
        with tempfile.TemporaryDirectory(prefix="muse-mcp-missing-") as directory:
            audit = Path(directory) / "audit.jsonl"
            process = subprocess.run([sys.executable, str(MODULE), "--venture", "synthetic-demo",
                "--timeout-seconds", "2", "--", sys.executable, str(FIXTURE), "missing", str(audit)],
                input="", capture_output=True, text=True, timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn("Traceback", process.stderr)
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(events, [{"event": "started"}])


if __name__ == "__main__":
    unittest.main()
