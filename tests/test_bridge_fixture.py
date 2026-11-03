"""Deterministic fixture-model tests for runtime-bridge-v1 (offline).

ONLY provider=fixture is used here: no network, no weights, no secrets.
The fixture stub drives standard harness tool-call round trips through the
real LangGraph prebuilt ReAct agent + persisted SqliteSaver checkpoint.

Live-model / renderer / sandbox checks live in test_live_optin.py and are
skipped unless explicitly enabled.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"


def run_bridge(request: dict, timeout_s: float = 90.0, extra_env=None):
    env = dict(os.environ)
    env.pop("ME_RUNTIME_SANDBOX", None)  # deterministic default: no sandbox
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(PY), "-m", "me_runtime.bridge"],
        input=json.dumps(request), capture_output=True, text=True,
        timeout=timeout_s, cwd=str(REPO), env=env)
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return proc, events


def base_request(ws: Path, **over) -> dict:
    req = {
        "bridge": "runtime-bridge-v1",
        "op": "run",
        "run_id": "run_test_1",
        "user_id": "alice",
        "conversation_id": "conv_a",
        "workspace": str(ws),
        "request": "hello",
        "capabilities": {"files": True, "web_fetch": False,
                         "browser": False, "code_exec": False,
                         "animation": False,
                         "network_egress_allowlist": []},
        "model": {"provider": "fixture", "model": "fixture-echo"},
        "budgets": {"max_steps": 8, "max_wall_seconds": 60,
                    "max_tool_calls": 20},
    }
    req.update(over)
    return req


def terminal(events):
    finals = [e for e in events if e.get("event") == "result"]
    assert len(finals) == 1, f"expected one terminal result: {events}"
    return finals[0]


def tool_calls(events, name=None):
    return [e for e in events if e.get("event") == "tool_call"
            and (name is None or e.get("tool") == name)]


class BridgeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "task1"
        (self.ws / "input").mkdir(parents=True)
        (self.ws / "input" / "notes.md").write_text("scoped notes alpha")

    def tearDown(self):
        self.tmp.cleanup()

    def test_completed_run_preserves_artifact(self):
        proc, events = run_bridge(base_request(self.ws, request="hello"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        term = terminal(events)
        self.assertEqual(term["status"], "completed")
        self.assertTrue(any(e["event"] == "started" for e in events))
        self.assertEqual(term["model"]["provider"], "fixture")
        paths = [a["path"] for a in term["artifacts"]]
        self.assertIn("output/result.md", paths)
        self.assertIn("fixture", (self.ws / "output" / "result.md")
                      .read_text())
        self.assertNotIn("bob", json.dumps(events))

    def test_iterative_read_reason_write_through_harness(self):
        """Proves multi-turn tool feedback: read content then write/check."""
        proc, events = run_bridge(
            base_request(self.ws, request="READWRITE: summarize the notes"))
        term = terminal(events)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(term["status"], "completed", term)
        reads = tool_calls(events, "files_read")
        writes = tool_calls(events, "files_write")
        self.assertGreaterEqual(len(reads), 1, events)
        self.assertGreaterEqual(len(writes), 1, events)
        ok_results = [e for e in events
                      if e.get("event") == "tool_result" and e.get("ok")]
        self.assertGreaterEqual(len(ok_results), 2, events)
        summary = (self.ws / "output" / "summary.md").read_text()
        self.assertIn("scoped notes alpha", summary)

    def test_waiting_input_then_resume(self):
        req = base_request(self.ws, request="ASK: what is the deadline?")
        _p, events = run_bridge(req)
        term = terminal(events)
        self.assertEqual(term["status"], "waiting_input")
        self.assertEqual(term["questions"][0]["id"], "q1")
        self.assertTrue((self.ws / ".me_runtime" / "pending.json").exists())
        self.assertTrue((self.ws / ".me_runtime" / "graph.db").exists())

        resume = base_request(
            self.ws, op="resume", inputs=[{"id": "q1", "answer": "Friday"}],
            request="ASK: what is the deadline?")
        proc2, events2 = run_bridge(resume)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        term2 = terminal(events2)
        self.assertEqual(term2["status"], "completed", term2)
        self.assertIn("Friday", term2["output"])
        # ask_user ran exactly once across both invocations: no replay.
        self.assertEqual(len(tool_calls(events2, "ask_user")), 1, events2)
        self.assertFalse((self.ws / ".me_runtime" / "pending.json").exists())

    def test_approval_approve_writes_record_once(self):
        req = base_request(self.ws, request="APPROVE: publish draft")
        _p, events = run_bridge(req)
        term = terminal(events)
        self.assertEqual(term["status"], "awaiting_approval")
        aid = term["approvals"][0]["id"]
        self.assertEqual(term["approvals"][0]["args"]["note"],
                         "publish draft")

        ok = base_request(self.ws, op="resume",
                          approvals=[{"id": aid, "decision": "approve"}],
                          request="APPROVE: publish draft")
        _p2, e2 = run_bridge(ok)
        term2 = terminal(e2)
        self.assertEqual(term2["status"], "completed", term2)
        record = self.ws / "output" / "approval_record.md"
        self.assertTrue(record.is_file())
        self.assertIn("publish draft", record.read_text())
        before = record.stat().st_mtime_ns

        # Replaying the same approval after completion is rejected: the
        # gated effect is not duplicated.
        _p3, e3 = run_bridge(ok)
        term3 = terminal(e3)
        self.assertEqual(term3["status"], "failed")
        self.assertEqual(term3["error"]["code"], "invalid_request")
        self.assertEqual(record.stat().st_mtime_ns, before)

    def test_approval_reject_is_honest_failure(self):
        run_bridge(base_request(self.ws, request="APPROVE: publish draft"))
        no = base_request(self.ws, op="resume",
                          approvals=[{"id": "a1", "decision": "reject"}],
                          request="APPROVE: publish draft")
        _p, events = run_bridge(no)
        term = terminal(events)
        self.assertEqual(term["status"], "failed")
        self.assertEqual(term["error"]["code"], "approval_rejected")
        self.assertFalse((self.ws / "output" / "approval_record.md").exists())

    def test_approval_changed_payload_rejected(self):
        run_bridge(base_request(self.ws, request="APPROVE: publish draft"))
        tampered = base_request(
            self.ws, op="resume",
            approvals=[{"id": "a1", "decision": "approve",
                        "args": {"note": "FORGED ACTION"}}],
            request="APPROVE: publish draft")
        _p, events = run_bridge(tampered)
        term = terminal(events)
        self.assertEqual(term["status"], "failed")
        self.assertEqual(term["error"]["code"], "invalid_request",
                         term)
        self.assertFalse((self.ws / "output" / "approval_record.md").exists())

    def test_approval_unknown_id_rejected(self):
        run_bridge(base_request(self.ws, request="APPROVE: publish draft"))
        wrong = base_request(
            self.ws, op="resume",
            approvals=[{"id": "nope", "decision": "approve"}],
            request="APPROVE: publish draft")
        proc, events = run_bridge(wrong)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(terminal(events)["error"]["code"],
                         "invalid_request")

    def test_honest_failure_never_fabricates_completion(self):
        proc, events = run_bridge(base_request(self.ws, request="FAIL:boom"))
        term = terminal(events)
        self.assertEqual(term["status"], "failed")
        self.assertEqual(term["error"]["code"], "tool_error")
        self.assertEqual(proc.returncode, 0)

    def test_run_id_reuse_rejected_no_blind_replay(self):
        req = base_request(self.ws, request="hello")
        _p, e1 = run_bridge(req)
        self.assertEqual(terminal(e1)["status"], "completed")
        proc2, e2 = run_bridge(req)
        self.assertEqual(proc2.returncode, 2)
        self.assertEqual(terminal(e2)["error"]["code"], "invalid_request")

    def test_host_code_never_runs_without_sandbox(self):
        sentinel = Path(self.tmp.name) / "sentinel-host-exec.txt"
        if sentinel.exists():
            sentinel.unlink()
        code = f"open({str(sentinel)!r}, 'w').write('pwned')"
        caps = dict(base_request(self.ws)["capabilities"])
        caps["code_exec"] = True
        # Case 1: sandbox not enabled at all.
        r1 = base_request(self.ws, request=f"RUN_CODE: {code}",
                          capabilities=caps)
        _p, e1 = run_bridge(r1)
        t1 = terminal(e1)
        self.assertEqual(t1["status"], "failed", t1)
        self.assertIn(t1["error"]["code"],
                      ("capability_denied", "prerequisite_missing"))
        # Case 2: sandbox flag set but no Docker on this host.
        ws2 = Path(self.tmp.name) / "task2"
        (ws2 / "input").mkdir(parents=True)
        r2 = base_request(ws2, request=f"RUN_CODE: {code}",
                          capabilities=caps, run_id="run_test_2")
        _p2, e2 = run_bridge(r2, extra_env={"ME_RUNTIME_SANDBOX": "docker"})
        t2 = terminal(e2)
        self.assertEqual(t2["status"], "failed", t2)
        self.assertIn(t2["error"]["code"],
                      ("capability_denied", "prerequisite_missing"))
        self.assertFalse(sentinel.exists(),
                         "host Python must never execute generated code")
        self.assertFalse((ws2 / "output" / "exec").exists())

    def test_egress_off_allowlist_denied_not_gated(self):
        req = base_request(self.ws, request="FETCH: https://example.com/x")
        _p, events = run_bridge(req)
        term = terminal(events)
        self.assertEqual(term["status"], "failed", term)
        self.assertEqual(term["error"]["code"], "capability_denied")

    def test_animation_builds_real_media(self):
        from PIL import Image  # noqa — proves engine present for this test
        _ = Image
        caps = dict(base_request(self.ws)["capabilities"])
        caps["animation"] = True
        req = base_request(self.ws, request="ANIMATE: fractions intro",
                           capabilities=caps)
        proc, events = run_bridge(req)
        term = terminal(events)
        self.assertEqual(term["status"], "completed", proc.stderr)
        gif = self.ws / "output" / "animation" / "animation.gif"
        self.assertTrue(gif.is_file())
        self.assertEqual(gif.read_bytes()[:6], b"GIF89a")
        self.assertTrue((self.ws / "output" / "animation" /
                         "scene.py").is_file())
        preview = (self.ws / "output" / "animation" /
                   "preview.html").read_text()
        self.assertIn('src="animation.gif"', preview)
        self.assertNotIn("data:image", preview)
        if shutil.which("ffmpeg"):
            self.assertTrue((self.ws / "output" / "animation" /
                             "animation.mp4").is_file())
        else:
            print("mp4 skipped: ffmpeg not installed (honest gap)")

    def test_animation_missing_engine_is_prerequisite_error(self):
        import me_runtime.tools.animate as anim
        real = sys.modules.get("PIL")
        sys.modules["PIL"] = None
        try:
            with self.assertRaises(Exception) as cm:
                anim.render_gif("t", "c", 2)
            self.assertIn("prerequisite_missing", str(cm.exception))
        finally:
            if real is not None:
                sys.modules["PIL"] = real
            else:
                del sys.modules["PIL"]

    def test_browser_missing_backend_is_prerequisite_error(self):
        import me_runtime.tools.web as webmod
        real_playwright = sys.modules.get("playwright")
        sys.modules["playwright"] = None
        real_getaddrinfo = socket.getaddrinfo

        def fake_public(host, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 0))]

        socket.getaddrinfo = fake_public
        try:
            from me_runtime.workspace import Workspace as _WS
            tools = webmod.make_web_tools(
                _WS(str(self.ws)),
                {"browser": True, "web_fetch": True,
                 "network_egress_allowlist": ["example.com"]},
                lambda p, d: None)
            browser_open = next(t for t in tools
                                if t.name == "browser_open")
            with self.assertRaises(Exception) as cm:
                browser_open.func(
                    "https://example.com/",
                    tool_call_id="test-tc-missing-backend")
            self.assertIn("prerequisite_missing", str(cm.exception))
        finally:
            socket.getaddrinfo = real_getaddrinfo
            if real_playwright is not None:
                sys.modules["playwright"] = real_playwright
            else:
                sys.modules.pop("playwright", None)

    def test_invalid_request_and_workspace(self):
        bad = {"bridge": "runtime-bridge-v1", "op": "run"}
        proc, events = run_bridge(bad)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(terminal(events)["status"], "failed")

        missing = base_request(Path(self.tmp.name) / "nope",
                               request="hello")
        proc2, events2 = run_bridge(missing)
        self.assertEqual(proc2.returncode, 2)
        self.assertEqual(terminal(events2)["error"]["code"],
                         "workspace_denied")

    def test_tool_allowlist_has_no_default_bypass_tools(self):
        import threading
        from me_runtime.agent import EXPECTED_TOOL_NAMES, RunTools, build_tools
        from me_runtime.workspace import Workspace
        ws = Workspace(str(self.ws))
        ctx = RunTools("t", "u", "c", threading.Event())
        tools = build_tools(ws, {k: True for k in
                                 ("files", "web_fetch", "browser",
                                  "code_exec", "animation")}, ctx)
        self.assertEqual(sorted(t.name for t in tools),
                         EXPECTED_TOOL_NAMES)
        lowered = " ".join(EXPECTED_TOOL_NAMES)
        for banned in ("shell", "bash", "terminal", "subagent", "computer"):
            self.assertNotIn(banned, lowered)

    def test_workspace_jail_unit(self):
        from me_runtime.workspace import Workspace, WorkspaceDenied
        ws = Workspace(str(self.ws))
        with self.assertRaises(WorkspaceDenied):
            ws.resolve("../escape.txt")
        with self.assertRaises(WorkspaceDenied):
            ws.resolve("/etc/passwd")

    def test_docker_cancel_helper_noop_without_docker(self):
        from me_runtime.tools.exec import kill_run_containers
        self.assertIsNone(kill_run_containers("run_test_cancel"))


if __name__ == "__main__":
    unittest.main()
