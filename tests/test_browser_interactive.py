"""R7 isolated interactive browser tests (offline, deterministic).

- protocol: bounded JSONL frames, action validation, fingerprints.
- broker_fetch_raw: binary/method/headers/body, credential-strip on
  cross-origin redirect, GET/HEAD default, write gate.
- container: locked-down flags, no host fallback.
- action gate: real harness approval/reject/stale/replay via the bridge
  with a fake `docker` executable speaking canned browser-worker-v1
  (no network, no real browser).
- isolation/cleanup/cancel: sessions scoped to run/user/conversation,
  storage under .me_runtime only, zero orphans.
- direct DOM smoke (opt-in, synthetic HTML, system Chrome): real
  Playwright button/text/screenshot through the worker + vetted broker.
  TEST-HARNESS ONLY, never production fallback.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import me_runtime.tools.web as webmod
import me_runtime.tools.exec as execmod
from me_runtime.tools import browser_protocol as proto
from me_runtime.tools import browser_host as bhost
from me_runtime.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"
ALLOW = ["example.com"]


def make_ws(tmp, name="btask"):
    ws = Path(tmp) / name
    (ws / "input").mkdir(parents=True)
    return Workspace(str(ws))


def run_bridge(request: dict, timeout_s: float = 90.0, extra_env=None):
    env = dict(os.environ)
    env.pop("ME_RUNTIME_SANDBOX", None)
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
        "run_id": "run_browser_1",
        "user_id": "alice",
        "conversation_id": "conv_a",
        "workspace": str(ws),
        "request": "hello",
        "capabilities": {"files": True, "web_fetch": True,
                         "browser": True, "code_exec": False,
                         "animation": False,
                         "network_egress_allowlist": ALLOW},
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


# ---------------------------------------------------------------------------
# Fake `docker` executable speaking canned browser-worker-v1 (no network).
# ---------------------------------------------------------------------------
FAKE_BROWSER_DOCKER = """#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("FAKE_DOCKER_LOG", "")
def logline(s):
    if log:
        with open(log, "a") as fh:
            fh.write(s + "\\n")
logline(" ".join(sys.argv))
if len(sys.argv) >= 3 and sys.argv[1] == "image":
    sys.exit(0)
if len(sys.argv) >= 2 and sys.argv[1] in ("kill", "rm"):
    sys.exit(0)
if len(sys.argv) >= 2 and sys.argv[1] == "run":
    fp_file = os.environ.get("FAKE_BROWSER_FP_FILE", "")
    def fp_default(url):
        if fp_file and os.path.exists(fp_file):
            try:
                return open(fp_file).read().strip()[:32] or "fp-stale-test"
            except Exception:
                pass
        return "fp-fixed-0123456789abcdef"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception:
            continue
        cid = cmd.get("id", 0)
        op = cmd.get("op", "")
        if op == "open":
            url = str(cmd.get("url", ""))[:2048]
            snap = {"url": url, "title": "Fake Page",
                    "body": "hello fake button world",
                    "actionables": [{"ref": "e0", "role": "button",
                                     "name": "Fake Button"}],
                    "fingerprint": fp_default(url),
                    "storage_state_b64": ""}
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": True, "payload": snap}) + "\\n")
            sys.stdout.flush()
        elif op == "snapshot":
            snap = {"url": "https://example.com/", "title": "Fake Page",
                    "body": "hello fake button world",
                    "actionables": [{"ref": "e0", "role": "button",
                                     "name": "Fake Button"}],
                    "fingerprint": fp_default(""),
                    "storage_state_b64": ""}
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": True, "payload": snap}) + "\\n")
            sys.stdout.flush()
        elif op == "act":
            snap = {"url": "https://example.com/done", "title": "Fake Page",
                    "body": "clicked fake button",
                    "actionables": [], "fingerprint": fp_default("act"),
                    "storage_state_b64": "",
                    "action_results": ["clicked e0"]}
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": True, "payload": snap}) + "\\n")
            sys.stdout.flush()
        elif op == "screenshot":
            snap = {"url": "https://example.com/", "title": "Fake Page",
                    "body": "hello", "actionables": [],
                    "fingerprint": fp_default("shot"),
                    "storage_state_b64": ""}
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": True, "payload": snap}) + "\\n")
            sys.stdout.flush()
        elif op == "close":
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": True,
                                          "payload": {"closed": True}}) + "\\n")
            sys.stdout.flush()
            break
        else:
            sys.stdout.write(json.dumps({"v": 1, "id": cid, "type": "result",
                                          "ok": False,
                                          "error": "unknown op"}) + "\\n")
            sys.stdout.flush()
    sys.exit(0)
sys.exit(1)
"""


def install_fake_docker(tmp: str):
    bindir = Path(tmp) / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_BROWSER_DOCKER)
    os.chmod(bindir / "docker",
              os.stat(bindir / "docker").st_mode
              | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log = Path(tmp) / "docker.log"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}",
           "FAKE_DOCKER_LOG": str(log)}
    return env, log


class ProtocolTests(unittest.TestCase):
    def test_frame_bounds(self):
        with self.assertRaises(ValueError):
            proto.encode_frame({"v": 1, "big": "x" * (proto.MAX_FRAME_BYTES + 1)})
        with self.assertRaises(ValueError):
            proto.decode_frame("x" * (proto.MAX_FRAME_BYTES + 1024))
        with self.assertRaises(ValueError):
            proto.validate_actions([{"op": "click", "target": "e0"}] * 9)
        with self.assertRaises(ValueError):
            proto.validate_actions([{"op": "dance", "target": "e0"}])
        with self.assertRaises(ValueError):
            proto.validate_actions([{"op": "fill", "target": "e0",
                                     "text": "x" * 5000}])
        clean = proto.validate_actions([{"op": "click", "target": "e0"},
                                        {"op": "scroll", "direction": "down",
                                         "pixels": 800}])
        self.assertEqual(len(clean), 2)

    def test_fingerprints_stable(self):
        a = proto.page_fingerprint("https://example.com/", "T", "body")
        b = proto.page_fingerprint("https://example.com/", "T", "body")
        c = proto.page_fingerprint("https://example.com/", "T", "other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        t1 = proto.target_fingerprint(a, "e0|button|Go|/a||||a")
        t2 = proto.target_fingerprint(a, "e0|button|Go|/b||||a")
        self.assertNotEqual(t1, t2)

    def test_same_body_different_destination_denied(self):
        acts_a = [{"ref": "e0", "role": "link", "name": "Checkout",
                   "href": "/pay-now", "formAction": "", "formMethod": "",
                   "inputType": "", "tag": "a"}]
        acts_b = [{"ref": "e0", "role": "link", "name": "Checkout",
                   "href": "/pay-later", "formAction": "", "formMethod": "",
                   "inputType": "", "tag": "a"}]
        fp_a = proto.page_fingerprint("https://example.com/", "Shop",
                                      "same visible body", acts_a)
        fp_b = proto.page_fingerprint("https://example.com/", "Shop",
                                      "same visible body", acts_b)
        self.assertNotEqual(fp_a, fp_b)
        d_a = proto.target_descriptor_for(acts_a, "e0")
        d_b = proto.target_descriptor_for(acts_b, "e0")
        self.assertNotEqual(d_a, d_b)
        self.assertNotEqual(proto.target_fingerprint(fp_a, d_a),
                            proto.target_fingerprint(fp_a, d_b))
        # Remapped ref (same destination, different element) also differs.
        acts_c = [{"ref": "e1", "role": "link", "name": "Checkout",
                   "href": "/pay-now", "formAction": "", "formMethod": "",
                   "inputType": "", "tag": "a"}]
        self.assertNotEqual(d_a, proto.target_descriptor_for(acts_c, "e1"))
        # Unknown ref yields an empty descriptor (reject on resume).
        self.assertEqual("", proto.target_descriptor_for(acts_a, "e9"))

    def test_b64_bounds(self):
        with self.assertRaises(ValueError):
            proto.b64d("!!!not-base64!!!")
        raw = proto.b64e(b"hi")
        self.assertEqual(base64.b64decode(raw.encode()), b"hi")


class BrokerRawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_needs_approved_window(self):
        with self.assertRaises(Exception) as cm:
            webmod.broker_fetch_raw("POST", "https://example.com/",
                                    ALLOW, {}, b"hi", False)
        self.assertIn("capability_denied", str(cm.exception))

    def test_private_ip_denied_no_fetch(self):
        calls = []
        real_run = webmod._run_curl
        real_res = webmod._resolve_addrs
        webmod._run_curl = lambda a, t, d=None: (calls.append(a), (0, b"", b""))[1]
        webmod._resolve_addrs = lambda h, p: ["10.0.0.5"]
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch_raw("GET", "https://example.com/",
                                        ALLOW, None, None, False)
            self.assertIn("capability_denied", str(cm.exception))
            self.assertEqual(calls, [])
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res

    def test_credential_strip_on_cross_origin_redirect(self):
        seen_headers = []

        def fake_run(args, timeout_s, input_data=None):
            # First hop: redirect example.com -> other.example.com
            # (both allowlisted for this test); second hop: 200.
            if len(seen_headers) == 0:
                seen_headers.append(list(args))
                out = (b"HTTP/1.1 302 Found\r\nLocation: "
                       b"https://other.example.com/next\r\n\r\n")
                return (0, out, b"")
            seen_headers.append(list(args))
            out = b"HTTP/1.1 200 OK\r\n\r\nok"
            return (0, out, b"")

        real_run = webmod._run_curl
        real_res = webmod._resolve_global
        webmod._run_curl = fake_run
        webmod._resolve_global = lambda h, p: ["93.184.216.34"]
        try:
            status, final, hdrs, body = webmod.broker_fetch_raw(
                "GET", "https://example.com/start",
                ["example.com", ".example.com"],
                {"Cookie": "sess=secret", "X-Ok": "1"}, None, False)
            self.assertEqual(status, 200)
            self.assertIn("other.example.com", final)
            # Second hop must not carry Cookie across origins.
            second = seen_headers[1]
            joined = " ".join(second)
            self.assertIn("X-Ok", joined)
            # Cookie header value must not appear on the redirected hop.
            # (First hop carries it; second must strip it.)
            first_has_cookie = any("Cookie" in a for a in seen_headers[0])
            second_has_cookie = any(a.startswith("Cookie:") for a in second)
            self.assertTrue(first_has_cookie)
            self.assertFalse(second_has_cookie)
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res

    def test_redirect_off_allowlist_blocked(self):
        def fake_run(args, timeout_s, input_data=None):
            return (0, b"HTTP/1.1 302 Found\r\nLocation: "
                    b"https://evil.example/\r\n\r\n", b"")

        real_run = webmod._run_curl
        real_res = webmod._resolve_global
        webmod._run_curl = fake_run
        webmod._resolve_global = lambda h, p: ["93.184.216.34"]
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch_raw("GET", "https://example.com/",
                                        ALLOW, None, None, False)
            self.assertIn("capability_denied", str(cm.exception))
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res


class FramerTests(unittest.TestCase):
    def test_partial_frame_accumulates(self):
        fr = bhost.JsonlFramer()
        self.assertEqual(fr.feed(b'{"v":1,"id":'), [])
        msgs = fr.feed(b'1,"type":"result","ok":true,"payload":{}}\n')
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["id"], 1)

    def test_batched_frames_all_returned_in_order(self):
        fr = bhost.JsonlFramer()
        chunk = (b'{"v":1,"id":1,"type":"result","ok":true,"payload":{}}\n'
                 b'{"v":1,"id":2,"type":"result","ok":true,"payload":{}}\n')
        msgs = fr.feed(chunk)
        self.assertEqual([m["id"] for m in msgs], [1, 2])

    def test_oversize_frame_rejected(self):
        fr = bhost.JsonlFramer(cap=64)
        with self.assertRaises(ValueError):
            fr.feed(b'{"v":1,"id":1,"pad":"' + b"x" * 128 + b'"}\n')

    def test_oversize_buffer_without_newline_rejected(self):
        fr = bhost.JsonlFramer(cap=64)
        with self.assertRaises(ValueError):
            for _ in range(4):
                fr.feed(b"x" * 512)

    def test_invalid_json_rejected(self):
        fr = bhost.JsonlFramer()
        with self.assertRaises(ValueError):
            fr.feed(b'not json\n')

    def test_blank_lines_skipped(self):
        fr = bhost.JsonlFramer()
        msgs = fr.feed(b'\n\n{"v":1,"id":7,"type":"result","ok":true,'
                       b'"payload":{}}\n\n')
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["id"], 7)


class CurlBodyTests(unittest.TestCase):
    def test_post_body_uses_data_binary_stdin(self):
        seen = {}

        def fake_run(args, timeout_s, input_data=None):
            seen["args"] = list(args)
            seen["input"] = input_data
            out = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok"
            return (0, out, b"")

        real_run = webmod._run_curl
        real_res = webmod._resolve_global
        webmod._run_curl = fake_run
        webmod._resolve_global = lambda h, p: ["93.184.216.34"]
        try:
            body = b"field1=value1&field2=value2"
            status, final, hdrs, payload = webmod.broker_fetch_raw(
                "POST", "https://example.com/submit", ALLOW,
                {"Content-Type": "application/x-www-form-urlencoded"},
                body, True)
            self.assertEqual(status, 200)
            self.assertEqual(payload, b"ok")
            args = seen["args"]
            self.assertIn("--data-binary", args)
            self.assertEqual(args[args.index("--data-binary") + 1], "@-")
            self.assertEqual(seen["input"], body)
            self.assertIn("-X", args)
            self.assertEqual(args[args.index("-X") + 1], "POST")
            joined = " ".join(args)
            self.assertIn("Content-Type", joined)
            # Raw body bytes never appear in argv.
            self.assertNotIn("field1=value1", joined)
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res

    def test_multipart_boundary_preserved(self):
        seen = {}

        def fake_run(args, timeout_s, input_data=None):
            seen["args"] = list(args)
            seen["input"] = input_data
            return (0, b"HTTP/1.1 200 OK\r\n\r\nsaved", b"")

        real_run = webmod._run_curl
        real_res = webmod._resolve_global
        webmod._run_curl = fake_run
        webmod._resolve_global = lambda h, p: ["93.184.216.34"]
        try:
            boundary = "----formdata1234"
            body = ("--%s\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n"
                    "v\r\n--%s--\r\n" % (boundary, boundary)).encode()
            status, _, _, _ = webmod.broker_fetch_raw(
                "POST", "https://example.com/upload", ALLOW,
                {"Content-Type":
                 "multipart/form-data; boundary=%s" % boundary},
                body, True)
            self.assertEqual(status, 200)
            self.assertEqual(seen["input"], body)
            self.assertIn(boundary, " ".join(seen["args"]))
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res

    def test_get_with_body_rejected(self):
        with self.assertRaises(Exception) as cm:
            webmod.broker_fetch_raw("GET", "https://example.com/",
                                    ALLOW, None, b"hi", True)
        self.assertIn("invalid_request", str(cm.exception))

    def test_oversize_body_rejected_before_write(self):
        with self.assertRaises(Exception) as cm:
            webmod.broker_fetch_raw("POST", "https://example.com/",
                                    ALLOW, None, b"x" * (256_000 + 1), True)
        self.assertIn("tool_error", str(cm.exception))


CANNED_WORKER_PY = (
    "import json,sys\n"
    "def w(o):\n"
    "    sys.stdout.buffer.write((json.dumps(o)+'\\n').encode())\n"
    "    sys.stdout.buffer.flush()\n"
    "n=0\n"
    "for raw in sys.stdin.buffer:\n"
    "    raw=raw.strip()\n"
    "    if not raw: continue\n"
    "    cmd=json.loads(raw.decode())\n"
    "    cid=cmd.get('id',0); op=cmd.get('op','')\n"
    "    if op=='open':\n"
    "        url=str(cmd.get('url',''))\n"
    "        w({'v':1,'id':cid,'type':'result','ok':True,'payload':"
    "{'url':url,'title':'Canned','body':'canned body',"
    "'actionables':[{'ref':'e0','role':'button','name':'Go',"
    "'href':'/go','formAction':'','formMethod':'',"
    "'inputType':'','tag':'a'}],"
    "'fingerprint':'fp-canned-1','storage_state_b64':''}})\n"
    "    elif op=='snapshot':\n"
    "        w({'v':1,'id':cid,'type':'result','ok':True,'payload':"
    "{'url':'https://example.com/','title':'Canned','body':'canned body',"
    "'actionables':[],'fingerprint':'fp-canned-1',"
    "'storage_state_b64':''}})\n"
    "    elif op=='act':\n"
    "        w({'v':1,'id':cid,'type':'result','ok':True,'payload':"
    "{'url':'https://example.com/done','title':'Canned',"
    "'body':'acted','actionables':[],'fingerprint':'fp-canned-2',"
    "'storage_state_b64':'','action_results':['ok']}})\n"
    "    elif op=='close':\n"
    "        w({'v':1,'id':cid,'type':'result','ok':True,"
    "'payload':{'closed':True}})\n"
    "        break\n"
    "    else:\n"
    "        w({'v':1,'id':cid,'type':'result','ok':False,"
    "'error':'unknown op'})\n"
)


def canned_factory():
    def _factory(session_id: str, cname: str):
        return subprocess.Popen(
            [sys.executable, "-c", CANNED_WORKER_PY],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
    return _factory


class ManagerReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        bhost.close_managers_for_run("run_reuse")

    def test_open_snapshot_act_share_one_manager(self):
        mgr = bhost.BrowserManager(
            self.ws, {"network_egress_allowlist": ["example.com"]},
            "run_reuse", "u", "c", lambda p, d: None, threading.Event(),
            popen_factory=canned_factory())
        try:
            sid, snap = mgr.open("https://example.com/", "",
                                 tool_call_id="tc-open-1")
            self.assertTrue(sid.startswith("br_run_reuse_"))
            snap2 = mgr.snapshot(sid)
            self.assertEqual(snap2["fingerprint"], "fp-canned-1")
            # Same underlying process reused: exactly one session/proc.
            self.assertEqual(list(mgr._sessions.keys()), [sid])
            first_proc = mgr._sessions[sid]
            res = mgr.act(sid, [{"op": "click", "target": "e0"}],
                          allow_write=True)
            self.assertIn("action_results", res)
            self.assertIs(mgr._sessions[sid], first_proc)
            # Second session is distinct with its own process.
            sid2, _ = mgr.open("https://example.com/other", "",
                               tool_call_id="tc-open-2")
            self.assertNotEqual(sid, sid2)
            self.assertEqual(len(mgr._sessions), 2)
            # FD cleanup: no owned processes survive close.
            mgr.close_all()
            self.assertEqual(mgr._sessions, {})
            self.assertIsNotNone(first_proc.popen.poll())
        finally:
            bhost.close_managers_for_run("run_reuse")

    def test_exchange_cancel_kills_without_hang(self):
        mgr = bhost.BrowserManager(
            self.ws, {"network_egress_allowlist": []},
            "run_reuse", "u", "c", lambda p, d: None, threading.Event(),
            popen_factory=canned_factory())
        try:
            proc = mgr._spawn("br_run_reuse_9")
            mgr.cancelled.set()
            try:
                with self.assertRaises(Exception) as cm:
                    mgr._exchange(proc, "snapshot", {}, False)
                self.assertIn("cancelled", str(cm.exception))
            finally:
                mgr.cancelled.clear()
        finally:
            bhost.close_managers_for_run("run_reuse")


class ContainerFlagTests(unittest.TestCase):
    def test_docker_cmd_locked_down(self):
        cmd = bhost.build_browser_docker_cmd("img:1", "me-br-x-y",
                                             "/srcdir")
        for flag, val in (("--network", "none"),
                          ("--user", "65532:65532"),
                          ("--cap-drop", "ALL"),
                          ("--security-opt", "no-new-privileges"),
                          ("--workdir", "/tmp"),
                          ("--pids-limit", "128")):
            self.assertIn(flag, cmd)
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("--read-only", cmd)
        self.assertIn("--pull", cmd)
        self.assertEqual(cmd[cmd.index("--pull") + 1], "never")
        self.assertIn("-v", cmd)
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        self.assertEqual(mounts, ["/srcdir:/worker:ro"])
        self.assertFalse(any(m.endswith(":rw") for m in mounts))
        joined = " ".join(cmd)
        self.assertNotIn("HOME/.", joined)
        self.assertIn("/worker/browser_worker.py", joined)


class ActionGateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "task1"
        (self.ws / "input").mkdir(parents=True)
        self.fake_env, self.log = install_fake_docker(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        bhost.close_managers_for_run("run_browser_1")
        bhost.close_managers_for_run("run_browser_2")

    def test_approve_executes_once(self):
        req = base_request(self.ws, request="BROWSER_ACT: https://example.com/")
        _p, events = run_bridge(req, extra_env=self.fake_env)
        term = terminal(events)
        self.assertEqual(term["status"], "awaiting_approval", term)
        aid = term["approvals"][0]["id"]
        self.assertEqual(term["approvals"][0]["action"], "browser_act")
        args = term["approvals"][0]["args"]
        self.assertIn("page_fingerprint", args)
        self.assertEqual(args["actions"][0]["op"], "click")

        ok = base_request(self.ws, op="resume",
                          approvals=[{"id": aid, "decision": "approve"}],
                          request="BROWSER_ACT: https://example.com/")
        _p2, e2 = run_bridge(ok, extra_env=self.fake_env)
        term2 = terminal(e2)
        self.assertEqual(term2["status"], "completed", term2)
        self.assertIn("clicked", term2["output"])
        # Pending consumed; storage stayed private (never an artifact).
        self.assertFalse((self.ws / ".me_runtime" / "pending.json").exists())
        for a in term2["artifacts"]:
            self.assertFalse(a["path"].startswith(".me_runtime/browser"))
            self.assertFalse(a["path"].startswith(".me_runtime"))

        # Replay of the same approval is rejected (no duplicate effect).
        _p3, e3 = run_bridge(ok, extra_env=self.fake_env)
        term3 = terminal(e3)
        self.assertEqual(term3["status"], "failed")
        self.assertEqual(term3["error"]["code"], "invalid_request")

    def test_reject_is_honest_failure(self):
        req = base_request(self.ws, request="BROWSER_ACT: https://example.com/")
        _p, events = run_bridge(req, extra_env=self.fake_env)
        aid = terminal(events)["approvals"][0]["id"]
        no = base_request(self.ws, op="resume",
                          approvals=[{"id": aid, "decision": "reject"}],
                          request="BROWSER_ACT: https://example.com/")
        _p2, e2 = run_bridge(no, extra_env=self.fake_env)
        term2 = terminal(e2)
        self.assertEqual(term2["status"], "failed")
        self.assertEqual(term2["error"]["code"], "approval_rejected")

    def test_changed_args_rejected(self):
        req = base_request(self.ws, request="BROWSER_ACT: https://example.com/")
        _p, events = run_bridge(req, extra_env=self.fake_env)
        term = terminal(events)
        aid = term["approvals"][0]["id"]
        forged = dict(term["approvals"][0]["args"])
        forged["actions"] = [{"op": "fill", "target": "e0",
                              "text": "FORGED"}]
        bad = base_request(self.ws, op="resume",
                           approvals=[{"id": aid, "decision": "approve",
                                       "args": forged}],
                           request="BROWSER_ACT: https://example.com/")
        _p2, e2 = run_bridge(bad, extra_env=self.fake_env)
        term2 = terminal(e2)
        self.assertEqual(term2["status"], "failed")
        self.assertEqual(term2["error"]["code"], "invalid_request")

    def test_stale_fingerprint_rejected(self):
        fp_file = Path(self.tmp.name) / "fp.txt"
        fp_file.write_text("fp-before-0000000000000000000000")
        env = dict(self.fake_env)
        env["FAKE_BROWSER_FP_FILE"] = str(fp_file)
        req = base_request(self.ws, request="BROWSER_ACT: https://example.com/")
        _p, events = run_bridge(req, extra_env=env)
        aid = terminal(events)["approvals"][0]["id"]
        # Page changes before the home approves: fresh fingerprint differs.
        fp_file.write_text("fp-after-ffffffffffffffffffffffff")
        ok = base_request(self.ws, op="resume",
                          approvals=[{"id": aid, "decision": "approve"}],
                          request="BROWSER_ACT: https://example.com/")
        _p2, e2 = run_bridge(ok, extra_env=env)
        term2 = terminal(e2)
        self.assertEqual(term2["status"], "failed", term2)
        self.assertEqual(term2["error"]["code"], "invalid_request")


class IsolationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_scoped_to_run_user_conv(self):
        mgr = bhost.BrowserManager(self.ws, {"network_egress_allowlist": []},
                                   "run_a", "alice", "conv_a",
                                   lambda p, d: None, threading.Event())
        try:
            sid = mgr.new_session_id()
            mgr._save_session(sid, "https://example.com/", "")
            # Same binding passes.
            mgr2 = bhost.BrowserManager(self.ws,
                                        {"network_egress_allowlist": []},
                                        "run_a", "alice", "conv_a",
                                        lambda p, d: None, threading.Event())
            mgr2._check_binding(sid)
            # Different run/user rejected.
            mgr3 = bhost.BrowserManager(self.ws,
                                        {"network_egress_allowlist": []},
                                        "run_b", "alice", "conv_a",
                                        lambda p, d: None, threading.Event())
            with self.assertRaises(Exception) as cm:
                mgr3._check_binding(sid)
            self.assertIn("invalid_request", str(cm.exception))
        finally:
            bhost.close_managers_for_run("run_a")
            bhost.close_managers_for_run("run_b")

    def test_cancel_kills_owned_containers(self):
        env, log = install_fake_docker(self.tmp.name)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = env["PATH"]
        os.environ["FAKE_DOCKER_LOG"] = env["FAKE_DOCKER_LOG"]
        try:
            bhost._register("run_cancel_test", "me-br-test-1")
            bhost.kill_run_browser_containers("run_cancel_test")
            logged = Path(env["FAKE_DOCKER_LOG"]).read_text()
            self.assertIn("kill me-br-test-1", logged)
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_DOCKER_LOG", None)

    def test_tool_call_keyed_allocation_reuse_and_isolation(self):
        mgr = bhost.BrowserManager(self.ws, {"network_egress_allowlist": []},
                                   "run_alloc", "u", "c",
                                   lambda p, d: None, threading.Event())
        try:
            first = mgr.new_session_id("tc-A")
            # Replaying the same tool call reproduces the identical id
            # (stable approval args_hash across interrupt resume).
            self.assertEqual(first, mgr.new_session_id("tc-A"))
            second = mgr.new_session_id("tc-B")
            self.assertNotEqual(first, second)
            # A fresh manager in a new process reloads the persisted map.
            mgr2 = bhost.BrowserManager(self.ws,
                                        {"network_egress_allowlist": []},
                                        "run_alloc", "u", "c",
                                        lambda p, d: None, threading.Event())
            self.assertEqual(first, mgr2.new_session_id("tc-A"))
            self.assertEqual(second, mgr2.new_session_id("tc-B"))
            third = mgr2.new_session_id("tc-C")
            self.assertNotEqual(third, first)
            self.assertNotEqual(third, second)
        finally:
            bhost.close_managers_for_run("run_alloc")

    def test_storage_never_an_artifact(self):
        mgr = bhost.BrowserManager(self.ws, {"network_egress_allowlist": []},
                                   "run_store", "u", "c",
                                   lambda p, d: None, threading.Event())
        try:
            sid = mgr.new_session_id()
            mgr._save_session(sid, "https://example.com/", "")
            self.assertTrue((mgr._session_dir(sid) / "meta.json").is_file())
            # Public file tools must deny the reserved browser state.
            from me_runtime.workspace import WorkspaceDenied
            pub = Workspace(str(self.ws.root))
            with self.assertRaises(WorkspaceDenied):
                pub.resolve_public(f".me_runtime/browser/{sid}/meta.json")
            with self.assertRaises(WorkspaceDenied):
                pub.read_text(f".me_runtime/browser/{sid}/meta.json")
        finally:
            bhost.close_managers_for_run("run_store")


def _system_chrome_executable() -> str | None:
    for cand in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/usr/bin/google-chrome", "/usr/bin/chromium"):
        try:
            if cand and Path(cand).exists():
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


@unittest.skipUnless(_system_chrome_executable() is not None,
                     "synthetic DOM smoke needs host system Chrome")
class DirectDomSmokeTests(unittest.TestCase):
    """Synthetic HTML through the real worker + system Chrome (no network).

    TEST-HARNESS ONLY: injects the worker via explicit `popen_factory`
    DI. Production never takes this path (Docker only, no env switch).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_dom_button_text_screenshot(self):
        import me_runtime.tools.browser_host as bhost_mod
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"playwright package not installed: {exc}")
        exe = _system_chrome_executable()
        if exe is None:
            self.skipTest("system chrome missing")
        html = (b"<html><head><title>Smoke</title></head><body>"
                b"<button>Press Me</button><p>synthetic smoke text</p>"
                b"</body></html>")
        real_raw = bhost_mod.broker_fetch_raw

        def fake_raw(method, url, allowlist, headers=None, body=None,
                     allow_write=False):
            return (200, url, {"content-type": "text/html"}, html)

        bhost_mod.broker_fetch_raw = fake_raw
        mgr = None
        try:
            mgr = bhost_mod.BrowserManager(
                self.ws, {"network_egress_allowlist": ["example.com"]},
                "run_smoke", "u", "c", lambda p, d: None, threading.Event(),
                popen_factory=bhost_mod.test_worker_popen_factory(exe))
            sid, snap = mgr.open("https://example.com/", "")
            self.assertEqual(snap["title"], "Smoke")
            self.assertIn("synthetic smoke text", snap["body"])
            names = " ".join(a.get("name", "") for a in snap["actionables"])
            self.assertIn("Press Me", names)
            shot = mgr.screenshot(sid)
            self.assertIn("screenshot_artifact", shot)
            art_path = self.ws.root / shot["screenshot_artifact"]
            self.assertTrue(art_path.is_file())
            self.assertEqual(art_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        finally:
            bhost_mod.broker_fetch_raw = real_raw
            if mgr is not None:
                try:
                    mgr.close_all()
                except Exception:  # noqa: BLE001
                    pass
            bhost.close_managers_for_run("run_smoke")


if __name__ == "__main__":
    unittest.main()
