"""Runtime delegation bridge tests with fake fd-3 pipe (offline).

No home server: socketpair fakes the duplex RPC. Proves bridge parks,
resumes, validates, and enforces completion policy. Real home integration
lives in tests/run_home_delegation.mjs (actual sibling API).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"


def base_request(ws: Path, **over):
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
    assert len(finals) == 1, f"expected one terminal: {events}"
    return finals[0]


class FakeHome(threading.Thread):
    """Minimal task-rpc-v1 responder on a socket (home-authoritative stub)."""

    def __init__(self, sock: socket.socket, mode="active"):
        super().__init__(daemon=True)
        self.sock = sock
        self.mode = mode  # active: children stay queued; terminal: succeeded
        self.children: dict[str, dict] = {}
        self.calls = 0
        self._stop_ev = threading.Event()
        self.buf = b""

    def run(self):
        self.sock.settimeout(0.2)
        while not self._stop_ev.is_set():
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                self.calls += 1
                rid = req.get("id")
                method = req.get("method")
                params = req.get("params", {})
                try:
                    res = self._handle(method, params)
                    resp = {"rpc": "task-rpc-v1", "id": rid, "ok": True,
                            "result": res}
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "code", "bad_request") \
                        if hasattr(exc, "code") else "bad_request"
                    # ToolDeniedError carries code; generic -> bad_request.
                    from me_runtime.errors import ToolDeniedError
                    if isinstance(exc, ToolDeniedError):
                        code = exc.code
                    resp = {"rpc": "task-rpc-v1", "id": rid, "ok": False,
                            "error": {"code": code,
                                      "message": str(exc)[:200]}}
                try:
                    self.sock.sendall((json.dumps(resp) + "\n").encode())
                except OSError:
                    return

    def _handle(self, method, params):
        from me_runtime.errors import ToolDeniedError
        if method == "submit_child":
            key = params.get("key", "")
            reqt = params.get("request", "")
            if not key or not reqt:
                raise ToolDeniedError("bad_request", "key/request required")
            # Idempotency: same key same hash -> same child; different -> conflict.
            import hashlib
            h = hashlib.sha256(json.dumps(
                {"request": reqt,
                 "capabilities": params.get("capabilities"),
                 "budgets": params.get("budgets"),
                 "title": params.get("title")},
                sort_keys=True).encode()).hexdigest()
            for c in self.children.values():
                if c["_key"] == key:
                    if c["_hash"] == h:
                        return {"child": {"id": c["id"],
                                          "status": c["status"]}}
                    raise ToolDeniedError(
                        "delegation_conflict",
                        "delegation key already used with different payload")
            cid = f"task_{key}"
            st = "queued" if self.mode == "active" else "succeeded"
            self.children[cid] = {"id": cid, "status": st,
                                   "_key": key, "_hash": h,
                                   "output": f"echo {reqt[:100]}"}
            # In terminal mode, children succeed immediately.
            if self.mode == "terminal":
                self.children[cid]["status"] = "succeeded"
            return {"child": {"id": cid, "status": self.children[cid]["status"]}}
        if method == "list_children":
            return {"children": [
                {"id": c["id"], "status": c["status"]}
                for c in self.children.values()]}
        if method == "get_child":
            cid = params.get("id", "")
            c = self.children.get(cid)
            if not c:
                raise ToolDeniedError("not_found", "Child not found")
            return {"child": {"id": c["id"], "status": c["status"]}}
        raise ToolDeniedError("bad_request", "Unknown rpc method")

    def stop(self):
        self._stop_ev.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def run_bridge_rpc(request: dict, fake: FakeHome | None,
                   parent_sock: socket.socket | None,
                   child_fd: int, timeout_s=30.0):
    env = dict(os.environ)
    env.pop("ME_RUNTIME_SANDBOX", None)
    env["ME_HOME_TASK_RPC_FD"] = str(child_fd)
    # pass_fds preserves the child fd number in the bridge process.
    proc = subprocess.run(
        [str(PY), "-m", "me_runtime.bridge"],
        input=json.dumps(request), capture_output=True, text=True,
        timeout=timeout_s, cwd=str(REPO), env=env,
        pass_fds=(child_fd,))
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return proc, events


class BridgeDelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "task1"
        (self.ws / "input").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_fd_prerequisite_missing(self):
        # No fd-3: delegate must fail honestly, legacy run still completes.
        req = base_request(self.ws, request="hello")
        env = dict(os.environ)
        env.pop("ME_HOME_TASK_RPC_FD", None)
        proc = subprocess.run(
            [str(PY), "-m", "me_runtime.bridge"],
            input=json.dumps(req), capture_output=True, text=True,
            timeout=30, cwd=str(REPO), env=env)
        events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(terminal(events)["status"], "completed")

        # Delegate without fd -> honest prerequisite_missing.
        req2 = base_request(self.ws, run_id="run_test_2",
                            request="DELEGATE:k1:hi\nDELEGATE_WAIT")
        (Path(self.tmp.name) / "task2").mkdir(parents=True)
        ws2 = Path(self.tmp.name) / "task2"
        (ws2 / "input").mkdir(parents=True, exist_ok=True)
        req2["workspace"] = str(ws2)
        proc2 = subprocess.run(
            [str(PY), "-m", "me_runtime.bridge"],
            input=json.dumps(req2), capture_output=True, text=True,
            timeout=30, cwd=str(REPO), env=env)
        e2 = [json.loads(l) for l in proc2.stdout.splitlines() if l.strip()]
        t2 = terminal(e2)
        self.assertEqual(t2["status"], "failed")
        self.assertEqual(t2["error"]["code"], "prerequisite_missing")

    def test_delegate_wait_park_resume(self):
        a, b = socket.socketpair()
        try:
            fake = FakeHome(a, mode="active")
            fake.start()
            req = base_request(self.ws,
                               request="DELEGATE:k1:hello child\nDELEGATE_WAIT")
            proc, events = run_bridge_rpc(req, fake, a, b.fileno())
            term = terminal(events)
            self.assertEqual(term["status"], "waiting_children", term)
            self.assertEqual(term["children"], ["task_k1"])
            self.assertTrue((self.ws / ".me_runtime" / "pending.json").exists())
            fake.stop()
            fake.join(timeout=2)
        finally:
            try:
                a.close()
            except OSError:
                pass
            try:
                b.close()
            except OSError:
                pass
        # Resume with child_results (terminal) -> completed aggregation.
        a2, b2 = socket.socketpair()
        try:
            fake2 = FakeHome(a2, mode="terminal")
            # Pre-seed terminal child so list after continuation sees terminal.
            fake2.children["task_k1"] = {
                "id": "task_k1", "status": "succeeded",
                "_key": "k1", "_hash": "x", "output": "echo hello child"}
            fake2.start()
            resume = base_request(
                self.ws, op="resume",
                request="DELEGATE:k1:hello child\nDELEGATE_WAIT",
                child_results=[{"id": "task_k1", "status": "succeeded",
                                "output": "echo hello child"}])
            proc2, events2 = run_bridge_rpc(resume, fake2, a2, b2.fileno())
            term2 = terminal(events2)
            self.assertEqual(term2["status"], "completed", term2)
            self.assertIn("task_k1", term2["output"])
            # Spawn idempotency: exactly one delegate call across resume
            # (no replay). Checkpointed tool calls replay once in events.
            calls = [e for e in events2 if e.get("event") == "tool_call"
                     and e.get("tool") == "delegate_task"]
            self.assertEqual(len(calls), 1, events2)
            self.assertFalse((self.ws / ".me_runtime" / "pending.json").exists())
            fake2.stop()
            fake2.join(timeout=2)
        finally:
            try:
                a2.close()
            except OSError:
                pass
            try:
                b2.close()
            except OSError:
                pass

    def test_completion_policy_early_completed_held(self):
        a, b = socket.socketpair()
        try:
            fake = FakeHome(a, mode="active")
            fake.start()
            req = base_request(
                self.ws,
                request="DELEGATE:k1:slow child\nCOMPLETE_EARLY")
            proc, events = run_bridge_rpc(req, fake, a, b.fileno())
            term = terminal(events)
            # Model returned completed early; bridge must hold as waiting.
            self.assertEqual(term["status"], "waiting_children", term)
            fake.stop()
            fake.join(timeout=2)
        finally:
            try:
                a.close()
            except OSError:
                pass
            try:
                b.close()
            except OSError:
                pass

    def test_resume_validation_rejects_unknown(self):
        # First park to create pending.
        a, b = socket.socketpair()
        try:
            fake = FakeHome(a, mode="active")
            fake.start()
            req = base_request(self.ws,
                               request="DELEGATE:k1:hi\nDELEGATE_WAIT")
            _p, _e = run_bridge_rpc(req, fake, a, b.fileno())
            fake.stop()
            fake.join(timeout=2)
        finally:
            try:
                a.close()
            except OSError:
                pass
            try:
                b.close()
            except OSError:
                pass
        # Unknown child id in resume -> invalid_request, no aggregation.
        a2, b2 = socket.socketpair()
        try:
            fake2 = FakeHome(a2, mode="terminal")
            fake2.start()
            bad = base_request(
                self.ws, op="resume",
                request="DELEGATE:k1:hi\nDELEGATE_WAIT",
                child_results=[{"id": "task_evil", "status": "succeeded",
                                "output": "x"}])
            # Use helper for correct pass_fds handling.
            proc, events = run_bridge_rpc(bad, fake2, a2, b2.fileno())
            term = terminal(events)
            self.assertEqual(term["status"], "failed")
            self.assertEqual(term["error"]["code"], "invalid_request")
            fake2.stop()
            fake2.join(timeout=2)
        finally:
            try:
                a2.close()
            except OSError:
                pass
            try:
                b2.close()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
