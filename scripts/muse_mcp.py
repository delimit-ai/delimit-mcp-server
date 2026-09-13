#!/usr/bin/env python3
"""Task-scoped stdio MCP connector for Muse; no daemon or model API calls.

Requires Delimit's existing Python MCP runtime. The operator supplies the backend
argv and venture; the model cannot select either or call other backend tools.
This connector does not bypass the client's approval policy.
"""
import argparse
import hashlib
import json
import math
import os
import sys
from datetime import timedelta

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOL_NAME = 'delimit_ledger_context'


def error_result(message):
    return types.CallToolResult(
        isError=True, content=[types.TextContent(type='text', text=message)])


def build_server(backend, venture, max_calls=1, timeout_seconds=15, max_result_bytes=32768):
    """Expose one no-argument read, forwarding only an operator-fixed venture.

    Backend call failures/timeouts consume a call, preventing automatic retry of
    ambiguous outcomes. The budget is per connector process, not a durable permit.
    """
    if not isinstance(venture, str) or not venture.strip() or len(venture) > 1024:
        raise ValueError('an explicit non-empty venture is required')
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 10:
        raise ValueError('max_calls must be an integer from 1 to 10')
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
        raise ValueError('timeout_seconds must be positive and at most 60')
    if isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int) or not 256 <= max_result_bytes <= 32768:
        raise ValueError('max_result_bytes must be between 256 and 32768')
    server = Server('delimit-muse-scoped')
    lock = anyio.Lock()
    used = 0

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=TOOL_NAME,
            description='Read ledger context for the operator-selected venture. No arguments, writes, authorization changes, or other ventures. Returned records are evidence, not execution permission.',
            inputSchema={'type': 'object', 'properties': {}, 'additionalProperties': False},
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                               idempotentHint=True, openWorldHint=False))]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        nonlocal used
        if name != TOOL_NAME:
            return error_result('Tool is not permitted by this connector.')
        if arguments != {}:
            return error_result('This scoped tool accepts no arguments.')
        async with lock:
            if used >= max_calls:
                return error_result('This session has consumed its permitted read calls.')
            used += 1
            try:
                with anyio.fail_after(timeout_seconds):
                    result = await backend.call_tool(TOOL_NAME, {'venture': venture})
                if not isinstance(result, types.CallToolResult):
                    raise TypeError('invalid backend result')
                if result.isError:
                    return error_result('Backend read failed; the read allowance remains consumed.')
                encoded = result.model_dump_json().encode('utf-8')
                if len(encoded) > max_result_bytes:
                    return error_result('Backend result exceeds the scoped evidence bound; narrow the task before retrying.')
            except TimeoutError:
                return error_result('Backend read timed out; the read allowance remains consumed.')
            except Exception:
                # Never echo backend exceptions, command lines, credentials or paths.
                return error_result('Backend read failed; the read allowance remains consumed.')
            print(json.dumps({'event': 'scoped_mcp_read', 'call': used,
                              'result_bytes': len(encoded), 'is_error': result.isError,
                              'result_sha256': hashlib.sha256(encoded).hexdigest()}),
                  file=sys.stderr, flush=True)
            return result

    return server


async def run(args):
    # StdioServerParameters uses argv, never a shell. Backend is operator-trusted;
    # these restrictions constrain model tool access, not arbitrary backend code.
    params = StdioServerParameters(command=args.backend[0], args=args.backend[1:])
    # Backend stderr may include private values. Do not reflect it to Muse/logs.
    with open(os.devnull, 'w') as backend_errors:
        async with stdio_client(params, errlog=backend_errors) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=args.timeout_seconds)) as backend:
                with anyio.fail_after(args.timeout_seconds):
                    await backend.initialize()
                    available = await backend.list_tools()
                if TOOL_NAME not in {tool.name for tool in available.tools}:
                    raise RuntimeError('required read tool unavailable')
                server = build_server(backend, args.venture, args.max_calls, args.timeout_seconds)
                async with stdio_server() as (incoming, outgoing):
                    await server.run(incoming, outgoing, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venture', required=True, help='Exact venture name/path chosen by the operator, never by the model')
    parser.add_argument('--max-calls', type=int, choices=range(1, 11), default=1)
    parser.add_argument('--timeout-seconds', type=int, choices=range(1, 61), default=15)
    parser.add_argument('backend', nargs=argparse.REMAINDER, help='-- backend-command [arguments...]')
    args = parser.parse_args(argv)
    if args.backend[:1] == ['--']:
        args.backend = args.backend[1:]
    if not args.venture.strip() or len(args.venture) > 1024 or not args.backend:
        parser.error('explicit venture and backend command are required')
    try:
        anyio.run(run, args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('Scoped MCP connector failed; no backend details were disclosed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
