#!/usr/bin/env python3
"""Delimit MCP Bridge Relay Server.

Bridges HTTP requests to a local MCP server running on stdin/stdout.
Designed for remote Claude Code sessions to proxy tool calls via SSH tunnel.

Usage: python mcp_bridge.py --command "path/to/mcp-server" --port 7824

Endpoints:
    GET  /health    - Health check
    GET  /mcp/list  - List available tools from the local MCP server
    POST /mcp/call  - Call a tool: {"method":"tools/call","params":{"name":"...","arguments":{...}}}
"""
import argparse, json, logging, select, subprocess, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("delimit.mcp_bridge")


class MCPSubprocessClient:
    """Manages a local MCP server subprocess communicating via JSON-RPC over stdio."""

    def __init__(self, command: str, init_timeout: float = 10.0):
        self.command = command
        self.init_timeout = init_timeout
        self._proc = None
        self._lock = threading.Lock()
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _write(self, msg: dict):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP subprocess not running")
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        self._proc.stdin.flush()

    def _send_notification(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _read(self, timeout: float = 30.0):
        if self._proc is None or self._proc.stdout is None:
            return None
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            logger.warning("Timeout waiting for MCP response")
            return None
        line = self._proc.stdout.readline()
        return json.loads(line.decode().strip()) if line else None

    def _send_request(self, method: str, params: dict, timeout: float = 30.0):
        with self._lock:
            req_id = self._next_id()
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return self._read(timeout=timeout)

    def start(self):
        self._proc = subprocess.Popen(
            self.command, shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        init_resp = self._send_request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "delimit-bridge", "version": "1.0.0"},
        })
        if init_resp is None:
            raise RuntimeError("MCP server did not respond to initialize")
        self._send_notification("notifications/initialized", {})
        logger.info("MCP server initialized: %s", init_resp.get("result", {}).get("serverInfo", {}))

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def list_tools(self) -> dict:
        return self._send_request("tools/list", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._send_request("tools/call", {"name": name, "arguments": arguments})


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that proxies requests to the MCP subprocess."""
    server_version = "DelimitMCPBridge/1.0"
    mcp_client: MCPSubprocessClient = None

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _mcp_result(self, resp, on_result=None):
        """Unwrap an MCP JSON-RPC response into an HTTP response."""
        if resp and "result" in resp:
            self._json(on_result(resp["result"]) if on_result else {"result": resp["result"]})
        elif resp and "error" in resp:
            self._json({"error": resp["error"]}, 502)
        else:
            self._json({"error": "no_response_from_mcp_server"}, 502)

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "server": "delimit-mcp-bridge"})
        elif self.path == "/mcp/list":
            try:
                self._mcp_result(self.mcp_client.list_tools(),
                                 on_result=lambda r: {"tools": r.get("tools", [])})
            except Exception as exc:
                logger.exception("list_tools failed")
                self._json({"error": str(exc)}, 500)
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/mcp/call":
            self._json({"error": "not_found"}, 404)
            return
        try:
            raw = self._body()
            if not raw:
                self._json({"error": "empty_body"}, 400); return
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json({"error": "invalid_json"}, 400); return
        params = body.get("params", {})
        tool_name = params.get("name")
        if not tool_name:
            self._json({"error": "missing params.name"}, 400); return
        try:
            self._mcp_result(self.mcp_client.call_tool(tool_name, params.get("arguments", {})))
        except Exception as exc:
            logger.exception("call_tool failed")
            self._json({"error": str(exc)}, 500)


def create_server(command: str, port: int = 7824, init_timeout: float = 10.0):
    """Create and return (httpd, mcp_client) without starting the serve loop."""
    client = MCPSubprocessClient(command, init_timeout=init_timeout)
    client.start()
    handler = type("Handler", (BridgeHandler,), {"mcp_client": client})
    return HTTPServer(("127.0.0.1", port), handler), client


def main():
    parser = argparse.ArgumentParser(description="Delimit MCP Bridge Relay")
    parser.add_argument("--command", required=True, help="Command to spawn the local MCP server")
    parser.add_argument("--port", type=int, default=7824, help="Port to listen on (default 7824)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("Starting MCP bridge: command=%r port=%d", args.command, args.port)
    httpd, client = create_server(args.command, args.port)
    try:
        logger.info("Bridge listening on 127.0.0.1:%d", args.port)
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        httpd.server_close()
        client.stop()


if __name__ == "__main__":
    main()
