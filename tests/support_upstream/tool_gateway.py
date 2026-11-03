"""TEST-SUPPORT loopback tool-callback gateway (devmode component tests only).

Not the production path: in production the OpenBot server owns
`/api/agent-tools/call`. This server speaks the same wire contract so
devmode tests prove the REAL vendored `execute_tools` HTTP path
(`{OPENBOT_TOOL_URL}/api/agent-tools/call`, header
`x-openbot-agent-token`, body `{name, args, run: <openbotRun assertion>}`)
against fixture-backed Me tool closures.

Security: per-run token + assertion are 256-bit secrets held in process
and pending.json (0600 workspace). They NEVER enter the LLM context,
graph checkpoint, AG-UI state snapshot, logs, or artifacts. Cross-run
replay is rejected (assertion bound to run_id).
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

MAX_BODY_BYTES = 256 * 1024


def new_run_credential() -> tuple[str, str]:
    """Return (tool_token, assertion) for one run."""
    return (secrets.token_urlsafe(32), secrets.token_urlsafe(32))


class Gateway:
    """Ephemeral 127.0.0.1 callback server bound to one run's tools."""

    def __init__(self, tool_token: str, assertion: str,
                 run_id: str, invoke: Any) -> None:
        self.tool_token = tool_token
        self.assertion = assertion
        self.run_id = run_id
        self._invoke = invoke
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN - silence stdlib logging
                pass

            def do_POST(self):
                try:
                    if self.path != "/api/agent-tools/call":
                        self._send(404, {"error": "not found"})
                        return
                    offered = (self.headers.get("x-openbot-agent-token")
                               or "").strip()
                    if not offered or not hmac.compare_digest(
                            offered.encode(), outer.tool_token.encode()):
                        self._send(401, {"error": "unauthorised"})
                        return
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > MAX_BODY_BYTES:
                        self._send(413, {"error": "body too large"})
                        return
                    try:
                        body = json.loads(
                            self.rfile.read(length).decode("utf-8"))
                    except Exception:  # noqa: BLE001
                        self._send(400, {"error": "invalid_request"})
                        return
                    if not isinstance(body, dict):
                        self._send(400, {"error": "invalid_request"})
                        return
                    if body.get("run") != outer.assertion:
                        self._send(403, {"error": "run mismatch"})
                        return
                    name = body.get("name")
                    args = body.get("args")
                    if not isinstance(name, str) or not name:
                        self._send(400,
                                   {"error": "invalid_request: tool name"})
                        return
                    if not isinstance(args, dict):
                        self._send(400,
                                   {"error": "invalid_request: args object"})
                        return
                    try:
                        text = outer._invoke(name, args)
                    except _Denied as exc:
                        # Honest denial: upstream surfaces HTTP code + error
                        # reason to the model as a ToolMessage, never raises.
                        self._send(exc.http,
                                   {"error": f"{exc.code}: {exc.message}"})
                        return
                    except Exception:  # noqa: BLE001
                        self._send(500, {"error": "tool_error"})
                        return
                    if not isinstance(text, str):
                        text = str(text)
                    self._send(200, {"text": text[:20000]})
                except Exception:  # noqa: BLE001 - never leak internals
                    try:
                        self._send(500, {"error": "tool_error"})
                    except Exception:  # noqa: BLE001
                        pass

            def _send(self, code: int, obj: dict) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = HTTPServer(("127.0.0.1", 0), _H)
        srv.daemon_threads = True
        self._server = srv
        port = srv.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self._thread = th
        return self.url

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
        self._server = None


class _Denied(Exception):
    def __init__(self, http: int, code: str, message: str) -> None:
        super().__init__(message)
        self.http = http
        self.code = code
        self.message = message[:500]
