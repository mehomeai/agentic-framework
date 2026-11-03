"""Production entrypoint tests: REAL upstream FastAPI app, no live spend.

Imports the actual vendored `src.main` (unmodified upstream
`agent-langgraph-agui` service: token middleware, health route, AG-UI
endpoint, graph, tool callback) from a hash-verified layout rebuilt from
in-repo vendored sources. Model + tool boundaries are loopback fakes;
no provider credentials, no Docker, no Intelligence, no license.

Proves: health contract, token middleware on the real app, AG-UI POST
through the real endpoint/graph/nodes, and real HTTP callback auth
(correct-token success vs missing/wrong-token 401 refusal with no
execution) — not a reconstructed topology. The loopback callback
fixture enforces its token like a deployment; labeled a protocol
fixture, not a full server-auth test. Environment is isolated so no
real CHATGPT store, OAuth, or provider credentials can leak in.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

VENDOR_SRC = ("main.py", "tool_runtime.py", "parallel_tools.py",
              "chatgpt_store.py")
VENDOR_SHARED = ("model_providers.py", "model-providers.json")
VENDOR_TESTS = ("conftest.py", "test_model_spec.py",
                "test_parallel_tool_events.py",
                "test_provider_boundaries.py", "test_spec_validation.py",
                "test_tool_protocol.py")


def _vendor_dir() -> Path:
    return (Path(__file__).resolve().parent.parent
            / "src" / "me_runtime" / "upstream" / "vendor")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_upstream_tree(root: Path) -> dict:
    """Rebuild the upstream repo layout from in-repo vendored files.

    root/
      shared/model_providers.py + model-providers.json
      agent-langgraph-agui/src/*.py
      agent-langgraph-agui/tests/*.py (upstream's own suite, verbatim)
    Every copied file is hash-checked against the in-repo vendored copy.
    """
    vendor = _vendor_dir()
    agent = root / "agent-langgraph-agui"
    src = agent / "src"
    tests = agent / "tests"
    shared = root / "shared"
    src.mkdir(parents=True)
    tests.mkdir(parents=True)
    shared.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for name in VENDOR_SRC:
        data = (vendor / name).read_bytes()
        (src / name).write_bytes(data)
        manifest[f"src/{name}"] = hashlib.sha256(data).hexdigest()
    for name in VENDOR_SHARED:
        data = (vendor / name).read_bytes()
        (shared / name).write_bytes(data)
        manifest[f"shared/{name}"] = hashlib.sha256(data).hexdigest()
    tdir = vendor.parent / "tests"
    for name in VENDOR_TESTS:
        data = (tdir / name).read_bytes()
        (tests / name).write_bytes(data)
        manifest[f"tests/{name}"] = hashlib.sha256(data).hexdigest()
    return {"root": str(root), "files": manifest}


class FakeOpenAI:
    """Deterministic OpenAI-compatible server (no live spend).

    Turn 1: emits one tool call per comma-separated name in the user text
    (only for offered tools). Turn 2+ (tool result present): final text.
    Records every request body + callback attempt for assertions.
    """

    def __init__(self, tool_names: list[str], callback_text: str,
                 force_calls: list[str] | None = None,
                 callback_token: str | None = None):
        self.tool_names = tool_names
        self.callback_text = callback_text
        # When set, turn 1 emits exactly these call names even if the RunAgentInput
        # did not offer them (proves the real UnofferedToolError boundary).
        self.force_calls = force_calls
        # When set, the callback endpoint enforces the token like a real
        # deployment: missing/wrong token -> 401/403 with NO execution.
        # (None = permissive legacy mode; new tests always enforce.)
        self.callback_token = callback_token
        self.model_bodies: list[dict] = []
        self.callbacks: list[dict] = []
        self.refusals: list[dict] = []
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode())
                if self.path == "/api/agent-tools/call":
                    offered = (self.headers.get("x-openbot-agent-token")
                               or "")
                    if outer.callback_token is not None and (
                            offered != outer.callback_token):
                        outer.refusals.append({"body": body, "token": offered})
                        code = 401 if not offered else 403
                        self.send_response(code)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(
                            {"error": "unauthorised"}).encode())
                        return
                    outer.callbacks.append({
                        "body": body,
                        "token": self.headers.get("x-openbot-agent-token"),
                    })
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(
                        {"text": outer.callback_text}).encode())
                    return
                outer.model_bodies.append(body)
                if self.path != "/v1/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return
                messages = body.get("messages", [])
                tools = body.get("tools", [])
                offered = {t["function"]["name"] for t in tools
                           if isinstance(t, dict) and "function" in t}
                last = messages[-1] if messages else {}
                if last.get("role") == "tool":
                    delta = {"role": "assistant",
                             "content": "final after tool result"}
                    finish = "stop"
                    parts = [(delta, None), ({}, finish)]
                else:
                    user = next((m.get("content", "") for m in reversed(messages)
                                 if m.get("role") == "user"), "")
                    if outer.force_calls is not None:
                        names = list(outer.force_calls)
                    else:
                        names = [n for n in user.split(",")
                                 if n in offered and n in outer.tool_names]
                    if not names:
                        delta = {"role": "assistant",
                                 "content": "no tool offered"}
                        parts = [(delta, None), ({}, "stop")]
                    else:
                        calls = [{"index": i, "id": f"call-{n}",
                                  "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(
                                      {"value": "marker"})}}
                                 for i, n in enumerate(names)]
                        parts = [({"role": "assistant", "tool_calls": [c]},
                                  None) for c in calls]
                        parts.append(({}, "tool_calls"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for part, reason in parts:
                    chunk = {"id": "proof", "object": "chat.completion.chunk",
                             "created": 0, "model": "proof",
                             "choices": [{"index": 0, "delta": part,
                                          "finish_reason": reason}]}
                    self.wfile.write(
                        ("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture(scope="function")
def tree(tmp_path_factory):
    from me_runtime.agent_service import verify_vendor_files
    # Provenance first: the in-repo vendor tree must match the trusted
    # MANIFEST (self-hash of staged-vs-vendor alone is not provenance).
    assert verify_vendor_files() == []
    root = tmp_path_factory.mktemp("openbot-tree")
    manifest = stage_upstream_tree(root)
    # Then verbatim-copy check: staged files equal the verified vendor.
    vendor = _vendor_dir()
    for name in VENDOR_SRC:
        assert _sha256(root / "agent-langgraph-agui" / "src" / name) == \
            _sha256(vendor / name), name
    for name in VENDOR_SHARED:
        assert _sha256(root / "shared" / name) == _sha256(vendor / name), name
    return root, manifest


@pytest.fixture(scope="function")
def upstream_app(tree, monkeypatch):
    root, _ = tree
    # Isolate from any real operator environment: no CHATGPT store,
    # OAuth material, provider keys/endpoints, or callback config may leak
    # in from the machine running the tests.
    for name in ("CHATGPT_AUTH_FILE", "CLAUDE_CODE_OAUTH_TOKEN",
                 "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_BASE_URL", "GOOGLE_API_KEY",
                 "GOOGLE_GENERATIVE_AI_API_KEY",
                 "GOOGLE_GENERATIVE_AI_BASE_URL", "OPENBOT_TOOL_URL",
                 "AGENT_TOOL_TOKEN", "MANAGED_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_PROVIDER", "openai")
    monkeypatch.setenv("BOT_MODEL", "entrypoint-proof")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    for mod in ("src", "src.main", "model_providers"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(root / "agent-langgraph-agui"))
    try:
        from src import main
        return main
    finally:
        try:
            sys.path.remove(str(root / "agent-langgraph-agui"))
        except ValueError:
            pass


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


def _agui_body(tool_names, deployment, assertion, thread=None):
    return {
        "threadId": thread or str(uuid4()),
        "runId": str(uuid4()),
        "messages": [{"id": "user-" + str(uuid4()), "role": "user",
                      "content": ",".join(tool_names)}],
        "tools": [{"name": n, "description": f"{n} tool",
                   "parameters": {"type": "object",
                                  "properties": {"value": {"type": "string"}}}}
                  for n in tool_names],
        "context": [],
        "state": {},
        "forwardedProps": {"openbotDeploymentTools": list(deployment),
                           "openbotRun": assertion},
    }


@pytest.mark.asyncio
async def test_health_contract_and_token_middleware(upstream_app, monkeypatch):
    main = upstream_app
    monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
    async with _client(main.app) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "harness": "langgraph"}
        # Token middleware: every non-health path requires the token.
        no_token = await client.post("/", json={})
        assert no_token.status_code == 401
        wrong = await client.post("/", json={},
                                  headers={"x-openbot-agent-token": "nope"})
        assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_agui_run_through_real_app_with_callback_auth(
        upstream_app, monkeypatch):
    main = upstream_app
    fake = FakeOpenAI(["computer_navigate"], "deployment result: marker 43",
                      callback_token="callback-token")
    base = fake.start()
    try:
        monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
        monkeypatch.setenv("OPENBOT_TOOL_URL", base + "/api/agent-tools/call")
        monkeypatch.setenv("AGENT_TOOL_TOKEN", "callback-token")
        body = _agui_body(["computer_navigate"], ["computer_navigate"],
                          "run-assertion-1")
        async with _client(main.app) as client:
            resp = await client.post(
                "/", json=body,
                headers={"x-openbot-agent-token": "entrypoint-token"})
        assert resp.status_code == 200
        events = [json.loads(line[6:]) for line in resp.text.splitlines()
                  if line.startswith("data: ")]
        assert events, "expected SSE events"
        assert events[0]["type"] == "RUN_STARTED"
        assert events[-1]["type"] == "RUN_FINISHED"
        assert not [e for e in events if e["type"] == "RUN_ERROR"], events
        # Real HTTP callback with exact token, body shape, run assertion.
        assert len(fake.callbacks) == 1
        cb = fake.callbacks[0]
        assert cb["token"] == "callback-token"
        assert cb["body"]["name"] == "computer_navigate"
        assert cb["body"]["args"] == {"value": "marker"}
        assert cb["body"]["run"] == "run-assertion-1"
        # Model saw the tool result on turn 2 (deployment path, real graph).
        tool_turns = [b for b in fake.model_bodies
                      if b.get("messages", [{}])[-1].get("role") == "tool"]
        assert tool_turns, "model never received the tool result"
    finally:
        fake.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("env_token", "want_code"),
                         [("wrong-token", "403"), ("", "401")])
async def test_wrong_callback_token_is_401_refusal_not_crash(
        upstream_app, monkeypatch, env_token, want_code):
    """Fixture callback rejects missing/wrong token with 401/403 and runs
    nothing: the real vendored `_call_tool` turns it into a
    model-visible refusal (HTTP 200 AG-UI run, RUN_FINISHED, no 500)."""
    main = upstream_app
    fake = FakeOpenAI(["computer_navigate"], "unreachable",
                      callback_token="callback-token")
    base = fake.start()
    try:
        monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
        monkeypatch.setenv("OPENBOT_TOOL_URL", base + "/api/agent-tools/call")
        if env_token:
            monkeypatch.setenv("AGENT_TOOL_TOKEN", env_token)
        else:
            monkeypatch.delenv("AGENT_TOOL_TOKEN", raising=False)
        body = _agui_body(["computer_navigate"], ["computer_navigate"],
                          "run-assertion-2")
        async with _client(main.app) as client:
            resp = await client.post(
                "/", json=body,
                headers={"x-openbot-agent-token": "entrypoint-token"})
        assert resp.status_code == 200
        events = [json.loads(line[6:]) for line in resp.text.splitlines()
                  if line.startswith("data: ")]
        assert events[-1]["type"] == "RUN_FINISHED"
        assert not [e for e in events if e["type"] == "RUN_ERROR"], events
        # Nothing executed. Wrong token: the fixture refused at the auth
        # boundary. Missing token: the real vendored `_call_tool` refuses
        # before any HTTP request (no credential to send).
        assert fake.callbacks == []
        if env_token:
            assert len(fake.refusals) == 1
        else:
            assert fake.refusals == []
        # The refusal text reached the model on turn 2 (no silent drop).
        tool_turns = [b for b in fake.model_bodies
                      if b.get("messages", [{}])[-1].get("role") == "tool"]
        assert tool_turns
        content = tool_turns[0]["messages"][-1]["content"]
        assert "Refused" in content
        if env_token:
            assert want_code in content
        else:
            assert "no credential" in content
    finally:
        fake.stop()


@pytest.mark.asyncio
async def test_unoffered_tool_is_run_error_not_callback_nor_crash(
        upstream_app, monkeypatch):
    """Real UnofferedToolError boundary: model calls a tool the run did not
    offer -> RUN_ERROR naming the refusal, zero callback POSTs, HTTP 200."""
    main = upstream_app
    fake = FakeOpenAI(["computer_navigate"], "never",
                      force_calls=["not_offered"])
    base = fake.start()
    try:
        monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
        monkeypatch.setenv("OPENBOT_TOOL_URL", base + "/api/agent-tools/call")
        monkeypatch.setenv("AGENT_TOOL_TOKEN", "callback-token")
        body = _agui_body(["computer_navigate"], ["computer_navigate"],
                          "run-assertion-3")
        async with _client(main.app) as client:
            resp = await client.post(
                "/", json=body,
                headers={"x-openbot-agent-token": "entrypoint-token"})
        assert resp.status_code == 200
        events = [json.loads(line[6:]) for line in resp.text.splitlines()
                  if line.startswith("data: ")]
        assert events[0]["type"] == "RUN_STARTED"
        err = [e for e in events if e["type"] == "RUN_ERROR"]
        assert err, events
        assert "not offered" in json.dumps(err).lower()
        assert fake.callbacks == []
    finally:
        fake.stop()


@pytest.mark.asyncio
async def test_run_secrets_never_reach_model_or_events(
        upstream_app, monkeypatch):
    """Checkpoint/request privacy on the real app: run assertion, server
    token, and callback token never appear in model request bodies or
    streamed AG-UI events (they live in the ContextVar, stripped from
    forwarded props and never checkpointed as content)."""
    main = upstream_app
    fake = FakeOpenAI(["computer_navigate"], "deployment result: marker 43",
                      callback_token="callback-token")
    base = fake.start()
    try:
        monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
        monkeypatch.setenv("OPENBOT_TOOL_URL", base + "/api/agent-tools/call")
        monkeypatch.setenv("AGENT_TOOL_TOKEN", "callback-token")
        body = _agui_body(["computer_navigate"], ["computer_navigate"],
                          "run-assertion-4")
        async with _client(main.app) as client:
            resp = await client.post(
                "/", json=body,
                headers={"x-openbot-agent-token": "entrypoint-token"})
        assert resp.status_code == 200
        model_blob = json.dumps(fake.model_bodies)
        for secret in ("run-assertion-4", "entrypoint-token",
                       "callback-token"):
            assert secret not in model_blob, secret
        # Stripped forwarded props: the deployment contract never reaches
        # the model as content/context either.
        assert "openbotRun" not in model_blob
        assert "openbotDeploymentTools" not in model_blob
        assert "run-assertion-4" not in resp.text
        assert "callback-token" not in resp.text
    finally:
        fake.stop()


@pytest.mark.asyncio
async def test_same_thread_resumes_across_runs(upstream_app, monkeypatch):
    """Native resume-by-threadId through the real endpoint: a second run on
    the same thread reuses the source checkpointer (MemorySaver) and runs
    the deployment tool again instead of erroring or replaying blindly."""
    main = upstream_app
    fake = FakeOpenAI(["computer_navigate"], "deployment result: marker 43",
                      callback_token="callback-token")
    base = fake.start()
    try:
        monkeypatch.setenv("MANAGED_AGENT_TOKEN", "entrypoint-token")
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
        monkeypatch.setenv("OPENBOT_TOOL_URL", base + "/api/agent-tools/call")
        monkeypatch.setenv("AGENT_TOOL_TOKEN", "callback-token")
        thread = str(uuid4())
        hdr = {"x-openbot-agent-token": "entrypoint-token"}
        async with _client(main.app) as client:
            for i in (1, 2):
                body = _agui_body(["computer_navigate"],
                                  ["computer_navigate"],
                                  f"run-assertion-resume-{i}", thread=thread)
                resp = await client.post("/", json=body, headers=hdr)
                assert resp.status_code == 200
                events = [json.loads(line[6:]) for line in
                          resp.text.splitlines() if line.startswith("data: ")]
                assert events[-1]["type"] == "RUN_FINISHED", events
        assert len(fake.callbacks) == 2
        assert {c["body"]["run"] for c in fake.callbacks} == \
            {"run-assertion-resume-1", "run-assertion-resume-2"}
    finally:
        fake.stop()


def test_ollama_compatible_route_config(monkeypatch):
    """Local-first Ollama via OpenAI-compatible base URL (verified, no net)."""
    from me_runtime.agent_service import (config_check, ollama_route,
                                          verify_ollama_model_object)
    env = {"MANAGED_AGENT_TOKEN": "t", "AGENT_TOOL_TOKEN": "t",
           **ollama_route("http://127.0.0.1:11434", "llama3.1")}
    assert env == {"MANAGED_AGENT_TOKEN": "t", "AGENT_TOOL_TOKEN": "t",
                   "BOT_PROVIDER": "openai",
                   "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1",
                   "BOT_MODEL": "llama3.1"}
    cfg = config_check({**env, "OPENAI_API_KEY": "ollama"})
    assert cfg["ok"], cfg["problems"]
    assert cfg["provider"] == "openai"
    proof = verify_ollama_model_object(env)
    assert proof["ok"], proof
    assert proof["model_class"] == "ChatOpenAI"
    assert proof["base_url"].startswith("http://127.0.0.1:11434/v1")
    # BOT_PROVIDER=ollama is NOT a supported config (no pretend support).
    bad = config_check({"MANAGED_AGENT_TOKEN": "t", "AGENT_TOOL_TOKEN": "t",
                        "BOT_PROVIDER": "ollama", "OPENAI_API_KEY": "x"})
    assert not bad["ok"]
    assert any("ollama" in p for p in bad["problems"])
