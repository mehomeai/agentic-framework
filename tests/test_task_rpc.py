"""Unit tests for fd-3 task-rpc-v1 client + brokered delegation tools.

Offline, no home required: socketpair fakes the duplex pipe. No secrets,
no network, no large packages.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest

from me_runtime.errors import ToolDeniedError
from me_runtime.task_rpc import TaskRpcClient, sanitize_child_env


def _fake_home(sock: socket.socket, handler, stop: threading.Event):
    """Batched/partial-safe fake home: reads JSONL, responds via handler."""
    buf = b""
    sock.settimeout(0.2)
    while not stop.is_set():
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            rid = req.get("id")
            try:
                result = handler(req)
                resp = {"rpc": "task-rpc-v1", "id": rid, "ok": True,
                        "result": result}
            except ToolDeniedError as exc:
                resp = {"rpc": "task-rpc-v1", "id": rid, "ok": False,
                        "error": {"code": exc.code,
                                  "message": exc.message[:200]}}
            except Exception as exc:  # noqa: BLE001
                resp = {"rpc": "task-rpc-v1", "id": rid, "ok": False,
                        "error": {"code": "bad_request", "message": str(exc)[:200]}}
            try:
                sock.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                return


class TaskRpcTests(unittest.TestCase):
    def _pair(self):
        a, b = socket.socketpair()
        return a, b

    def test_happy_path_id_correlation(self):
        a, b = self._pair()
        stop = threading.Event()
        kids = {}
        def h(req):
            m = req.get("method")
            if m == "submit_child":
                cid = f"task_{req['params']['key']}"
                kids[cid] = {"id": cid, "status": "queued"}
                return {"child": kids[cid]}
            if m == "list_children":
                return {"children": list(kids.values())}
            if m == "get_child":
                cid = req["params"]["id"]
                if cid not in kids:
                    raise ToolDeniedError("not_found", "Child not found")
                return {"child": kids[cid]}
            raise ToolDeniedError("bad_request", "Unknown rpc method")
        t = threading.Thread(target=_fake_home, args=(b, h, stop), daemon=True)
        t.start()
        # Pass fd number: dup the socket fd for the client.
        client = TaskRpcClient(a.fileno(), threading.Event())
        try:
            r1 = client.call("submit_child", {"key": "k1", "request": "hi"})
            self.assertEqual(r1["child"]["id"], "task_k1")
            r2 = client.call("list_children", {})
            self.assertEqual(len(r2["children"]), 1)
            r3 = client.call("get_child", {"id": "task_k1"})
            self.assertEqual(r3["child"]["status"], "queued")
            # Unknown/cross-scope -> denial, no fallback.
            with self.assertRaises(ToolDeniedError) as cm:
                client.call("get_child", {"id": "task_nope"})
            self.assertEqual(cm.exception.code, "not_found")
        finally:
            client.close()
            stop.set()
            a.close()
            b.close()
            t.join(timeout=2)

    def test_partial_batched_utf8(self):
        a, b = self._pair()
        stop = threading.Event()
        def h(req):
            return {"children": []}
        t = threading.Thread(target=_fake_home, args=(b, h, stop), daemon=True)
        t.start()
        client = TaskRpcClient(a.fileno(), threading.Event())
        try:
            # Batched concurrent calls serialize with correct ID routing.
            out = {}
            def _call(i):
                out[i] = client.call("list_children", {})
            ths = [threading.Thread(target=_call, args=(i,)) for i in range(5)]
            for th in ths:
                th.start()
            for th in ths:
                th.join(timeout=5)
            self.assertEqual(len(out), 5)
            for v in out.values():
                self.assertIn("children", v)
            # Split emoji across chunks still decodes (client reader uses
            # incremental decoder; fake home sends emoji in result).
            stop2 = threading.Event()
            b2a, b2b = self._pair()
            def h2(req):
                return {"echo": "😀" * 10}
            t2 = threading.Thread(target=_fake_home, args=(b2b, h2, stop2),
                                  daemon=True)
            t2.start()
            c2 = TaskRpcClient(b2a.fileno(), threading.Event())
            try:
                r = c2.call("list_children", {})
                self.assertIn("😀", json.dumps(r, ensure_ascii=False))
            finally:
                c2.close()
                stop2.set()
                b2a.close()
                b2b.close()
        finally:
            client.close()
            stop.set()
            a.close()
            b.close()

    def test_bounds_params_line_budget(self):
        a, b = self._pair()
        stop = threading.Event()
        t = threading.Thread(target=_fake_home,
                             args=(b, lambda r: {}, stop), daemon=True)
        t.start()
        client = TaskRpcClient(a.fileno(), threading.Event())
        try:
            with self.assertRaises(ToolDeniedError) as cm:
                client.call("submit_child",
                            {"key": "k", "request": "x",
                             "pad": "y" * (33 * 1024)})
            self.assertEqual(cm.exception.code, "invalid_request")
            # 200 request budget: 201st denied locally, no hang.
            for _ in range(200):
                client.call("list_children", {})
            with self.assertRaises(ToolDeniedError) as cm2:
                client.call("list_children", {})
            self.assertEqual(cm2.exception.code, "delegation_denied")
        finally:
            client.close()
            stop.set()
            a.close()
            b.close()

    def test_missing_fd_prerequisite(self):
        with self.assertRaises(ToolDeniedError) as cm:
            TaskRpcClient(9999, threading.Event())
        self.assertEqual(cm.exception.code, "prerequisite_missing")
        # Regular file is not a duplex socket/pipe.
        with tempfile.NamedTemporaryFile() as tf:
            with self.assertRaises(ToolDeniedError) as cm2:
                TaskRpcClient(tf.fileno(), threading.Event())
            self.assertEqual(cm2.exception.code, "prerequisite_missing")

    def test_sanitize_env_no_inherit_no_log(self):
        os.environ["ME_HOME_TASK_RPC_FD"] = "3"
        os.environ["https_proxy"] = "http://proxy.invalid/"
        env = sanitize_child_env()
        self.assertNotIn("ME_HOME_TASK_RPC_FD", env)
        self.assertNotIn("https_proxy", env)
        self.assertNotIn("HTTPS_PROXY", env)
        del os.environ["ME_HOME_TASK_RPC_FD"]
        del os.environ["https_proxy"]
        # Error messages never carry request contents/credentials.
        a, b = self._pair()
        stop = threading.Event()
        secret = "SECRET-ABC-123"
        def h(req):
            raise ToolDeniedError("capability_denied", "denied")
        t = threading.Thread(target=_fake_home, args=(b, h, stop), daemon=True)
        t.start()
        client = TaskRpcClient(a.fileno(), threading.Event())
        try:
            with self.assertRaises(ToolDeniedError) as cm:
                client.call("submit_child",
                            {"key": "k", "request": secret})
            self.assertNotIn(secret, str(cm.exception))
            self.assertNotIn(secret, cm.exception.message)
        finally:
            client.close()
            stop.set()
            a.close()
            b.close()

    def test_cancellation_deadline(self):
        a, b = self._pair()
        # No fake home reader: call must time out, not hang on readline.
        client = TaskRpcClient(a.fileno(), threading.Event(),
                               default_timeout=1.0)
        try:
            with self.assertRaises(ToolDeniedError) as cm:
                client.call("list_children", {}, timeout=1.0)
            self.assertIn(cm.exception.code,
                          ("tool_error", "wrong_state", "cancelled"))
        finally:
            client.close()
            a.close()
            b.close()
        # Cancelled event aborts fast.
        a2, b2 = self._pair()
        cancelled = threading.Event()
        cancelled.set()
        c2 = TaskRpcClient(a2.fileno(), cancelled)
        try:
            with self.assertRaises(ToolDeniedError) as cm2:
                c2.call("list_children", {})
            self.assertEqual(cm2.exception.code, "cancelled")
        finally:
            c2.close()
            a2.close()
            b2.close()

    def test_tool_allowlist_has_delegation(self):
        import threading as _th
        from me_runtime.agent import EXPECTED_TOOL_NAMES, RunTools, build_tools
        from me_runtime.workspace import Workspace
        tmp = tempfile.TemporaryDirectory()
        wsdir = tempfile.mkdtemp(dir=tmp.name)
        # Need a real workspace dir.
        import pathlib
        p = pathlib.Path(wsdir)
        ws = Workspace(str(p))
        ctx = RunTools("t", "u", "c", _th.Event())
        tools = build_tools(ws,
                            {k: True for k in
                             ("files", "web_fetch", "browser",
                              "code_exec", "animation")}, ctx)
        self.assertEqual(sorted(t.name for t in tools),
                         EXPECTED_TOOL_NAMES)
        for name in ("delegate_task", "list_children", "get_child",
                     "await_children"):
            self.assertIn(name, EXPECTED_TOOL_NAMES)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
