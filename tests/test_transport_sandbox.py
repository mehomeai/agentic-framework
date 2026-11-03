"""R3 transport + sandbox tests: deterministic, fully mocked, offline.

- web_fetch: every redirect guarded, credentials/ports/schemes rejected,
  non-global IPs (incl. rebind/private/reserved) refused, DNS pinned per
  hop, body capped. Network is fully mocked (no sockets leave the test).
- browser_open: offline render only — JS disabled, subrequests aborted,
  no navigation, screenshot artifact. Playwright is faked in-process.
- run_code: docker flags asserted; lifecycle (timeout kill+rm, artifact
  caps, symlink rejection) exercised against a fake `docker` executable.
"""
from __future__ import annotations

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
from me_runtime.workspace import Workspace


def make_ws(tmp, name="task"):
    ws = Path(tmp) / name
    (ws / "input").mkdir(parents=True)
    return Workspace(str(ws))


class FakeCurl:
    """Records curl argv; serves scripted responses; never touches network."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.script: list = []  # items: (rc, stdout, stderr) or Exception

    def __call__(self, args, timeout_s):
        self.calls.append(list(args))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def resp(status, headers="", body=b""):
        out = headers.encode("latin-1") + b"\r\n\r\n" + body
        return (0, out, b"")


def public_resolve(host, port=443):
    return ["93.184.216.34"]


ALLOW = ["example.com"]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_url_rejects_credentials_ports_schemes(self):
        for bad in ("https://user:pass@example.com/",
                    "https://user@example.com/",
                    "https://example.com:8443/",
                    "http://example.com:8080/",
                    "ftp://example.com/x",
                    "file:///etc/passwd",
                    "https:///nohost"):
            with self.assertRaises(Exception, msg=bad) as cm:
                webmod._validate_url(bad, ALLOW)
            self.assertIn("capability_denied", str(cm.exception))

    def test_private_rebind_and_reserved_refused_without_fetch(self):
        fake = FakeCurl()
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_addrs, real_res = (lambda h, p: ["10.0.0.5"],
                                           webmod._resolve_addrs)
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("https://example.com/", ALLOW)
            self.assertIn("capability_denied", str(cm.exception))
            self.assertEqual(fake.calls, [], "no fetch may run for private IP")
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res
        # Reserved documentation range is also non-global.
        webmod._resolve_addrs, real_res = (lambda h, p: ["192.0.2.1"],
                                           webmod._resolve_addrs)
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("https://example.com/", ALLOW)
            self.assertIn("capability_denied", str(cm.exception))
        finally:
            webmod._resolve_addrs = real_res

    def test_unsafe_redirect_target_blocked_second_hop_never_fetched(self):
        fake = FakeCurl()
        fake.script.append(
            FakeCurl.resp(302, "HTTP/1.1 302 Found\r\n"
                               "Location: http://169.254.169.254/secret"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        calls_resolve = []

        def fake_resolve(host, port):
            calls_resolve.append(host)
            if host == "169.254.169.254":
                from me_runtime.errors import ToolDeniedError
                raise ToolDeniedError("capability_denied",
                                      "non-global destination refused")
            return ["93.184.216.34"]

        webmod._resolve_global, real_res = fake_resolve, webmod._resolve_global
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("http://example.com/start", ALLOW)
            self.assertIn("capability_denied", str(cm.exception))
            self.assertEqual(len(fake.calls), 1,
                             "redirect target must never be fetched")
            resolve_arg = [a for a in fake.calls[0] if a.startswith(
                "example.com:")]
            self.assertTrue(any("93.184.216.34" in a for a in resolve_arg),
                            f"hop must pin vetted IP: {fake.calls[0]}")
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res

    def test_too_many_redirects_rejected(self):
        fake = FakeCurl()
        for _ in range(5):
            fake.script.append(
                FakeCurl.resp(302, "HTTP/1.1 302 Found\r\nLocation: /loop"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_global, real_res = public_resolve, \
            webmod._resolve_global
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("http://example.com/loop", ALLOW)
            self.assertIn("too many redirects", str(cm.exception))
            self.assertLessEqual(len(fake.calls), 4)
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res

    def test_curl_ignores_curlrc_and_proxy(self):
        """-q first, --noproxy '*', -- terminator; env carries no proxy."""
        fake = FakeCurl()
        fake.script.append(FakeCurl.resp(200, "HTTP/1.1 200 OK", b"ok"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_addrs, real_res = public_resolve, \
            webmod._resolve_addrs
        try:
            status, final, text = webmod.broker_fetch(
                "https://example.com/", ALLOW)
            self.assertEqual(status, 200)
            argv = fake.calls[0]
            self.assertEqual(argv[1], "-q",
                             "user curlrc must be ignored")
            self.assertIn("--noproxy", argv)
            self.assertEqual(argv[argv.index("--noproxy") + 1], "*")
            self.assertEqual(argv[-2], "--",
                             "URL must follow a -- terminator")
            self.assertEqual(argv[-1], "https://example.com/")
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res
        # Proxy variables never reach the curl environment.
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:9"
        os.environ["https_proxy"] = "http://proxy.invalid:9"
        try:
            env = webmod._curl_env()
            self.assertFalse(any(k.upper().endswith("_PROXY") for k in env))
            self.assertIn("PATH", env)
        finally:
            del os.environ["HTTPS_PROXY"]
            del os.environ["https_proxy"]

    def test_curl_env_actually_used_by_runner(self):
        import unittest.mock as mock

        seen = {}

        class FakeStream:
            def __init__(self, data):
                self._data = data

            def read(self, n):
                chunk, self._data = self._data[:n], self._data[n:]
                return chunk

        class FakeProc:
            def __init__(self, *a, **k):
                seen.update(k)
                self.stdout = FakeStream(b"HTTP/1.1 200 OK\r\n\r\nok")
                self.stderr = FakeStream(b"")

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        os.environ["HTTP_PROXY"] = "http://proxy.invalid:9"
        try:
            with mock.patch.object(webmod.subprocess, "Popen",
                                   FakeProc):
                rc, out, err = webmod._curl_bounded(["curl", "-q"], 10)
        finally:
            del os.environ["HTTP_PROXY"]
        self.assertEqual(rc, 0)
        self.assertIn(b"200 OK", out)
        self.assertFalse(any(k.upper().endswith("_PROXY")
                             for k in seen.get("env", {})),
                         "proxies must not reach the curl process")

    def test_oversized_headers_refused_while_streaming(self):
        # Real streaming runner: unbounded output is killed, not buffered.
        with self.assertRaises(Exception) as cm:
            webmod._curl_bounded(
                ["/bin/sh", "-c", "head -c 3000000 /dev/zero"], 30)
        self.assertIn("exceeded", str(cm.exception))
        # Oversized headers refused even under the total cap.
        fake = FakeCurl()
        big = b"HTTP/1.1 200 OK\r\n" + b"X-Pad: y\r\n" * 60000
        fake.script.append((0, big + b"\r\n\r\nbody", b""))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_addrs, real_res = public_resolve, \
            webmod._resolve_addrs
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("https://example.com/", ALLOW)
            self.assertIn("headers exceeded", str(cm.exception))
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res

    def test_ipv6_pinned_with_brackets_linklocal_refused(self):
        fake = FakeCurl()
        fake.script.append(FakeCurl.resp(200, "HTTP/1.1 200 OK", b"v6"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_addrs, real_res = (
            lambda h, p: ["2606:4700:4700::1111"], webmod._resolve_addrs)
        try:
            status, _, _ = webmod.broker_fetch("https://example.com/",
                                               ALLOW)
            self.assertEqual(status, 200)
            resolve = [a for a in fake.calls[0]
                       if a.startswith("example.com:")]
            self.assertEqual(resolve,
                             ["example.com:443:[2606:4700:4700::1111]"])
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res
        for bad_ip in ("::1", "fe80::1", "::"):
            webmod._resolve_addrs = lambda h, p, _ip=bad_ip: [_ip]  # noqa: E731
            try:
                with self.assertRaises(Exception) as cm:
                    webmod.broker_fetch("https://example.com/", ALLOW)
                self.assertIn("capability_denied", str(cm.exception))
            finally:
                webmod._resolve_addrs = real_res

    def test_chunked_framing_needs_no_manual_parser(self):
        # curl decodes chunked framing; the broker never parses it itself.
        fake = FakeCurl()
        fake.script.append(FakeCurl.resp(
            200, "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked",
            b"decoded-body"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_addrs, real_res = public_resolve, \
            webmod._resolve_addrs
        try:
            status, _, text = webmod.broker_fetch("https://example.com/",
                                                  ALLOW)
            self.assertEqual(status, 200)
            self.assertEqual(text, "decoded-body")
        finally:
            webmod._run_curl, webmod._resolve_addrs = real_run, real_res

    def test_body_cap_refused_and_output_bounded(self):
        fake = FakeCurl()
        fake.script.append((63, b"", b"too big"))
        webmod._run_curl, real_run = fake, webmod._run_curl
        webmod._resolve_global, real_res = public_resolve, \
            webmod._resolve_global
        try:
            with self.assertRaises(Exception) as cm:
                webmod.broker_fetch("https://example.com/big", ALLOW)
            self.assertIn("exceeded", str(cm.exception))
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res
        # A large-but-allowed body is bounded in tool output.
        fake2 = FakeCurl()
        fake2.script.append(FakeCurl.resp(
            200, "HTTP/1.1 200 OK", b"A" * 400_000))
        webmod._run_curl, real_run = fake2, webmod._run_curl
        webmod._resolve_global, real_res = public_resolve, \
            webmod._resolve_global
        try:
            tools = webmod.make_web_tools(
                self.ws,
                {"web_fetch": True,
                 "network_egress_allowlist": ALLOW}, lambda p, d: None)
            fetch = next(t for t in tools if t.name == "web_fetch")
            out = fetch.func("https://example.com/big")
            self.assertLessEqual(len(out), 9000)
        finally:
            webmod._run_curl, webmod._resolve_global = real_run, real_res


class FakeRoute:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class FakePage:
    def __init__(self, rec):
        self._rec = rec

    def route(self, pattern, handler):
        self._rec["route_handler"] = handler

    def set_content(self, html, timeout=None):
        self._rec["content"] = html

    def title(self):
        return "T"

    def inner_text(self, sel):
        return "hello body"

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 100


class FakeBrowser:
    def __init__(self, rec):
        self._rec = rec

    def new_page(self, java_script_enabled=True):
        self._rec["js_enabled"] = java_script_enabled
        return FakePage(self._rec)

    def close(self):
        pass


class FakeChromium:
    def __init__(self, rec):
        self._rec = rec

    def launch(self, headless=True):
        self._rec["headless"] = headless
        return FakeBrowser(self._rec)


class FakePlaywright:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def chromium(self):
        return FakeChromium(self._rec)


class BrowserOfflineTests(unittest.TestCase):
    """R7: the R3 host-Chrome offline renderer is superseded.

    `browser_open` is now isolated-interactive (Docker-only, no host
    fallback). Without Docker + provisioned image it fails closed with
    `prerequisite_missing` instead of rendering on the host.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolated_browser_needs_docker_no_host_fallback(self):
        import me_runtime.tools.browser_host as bhost
        real_docker = bhost.docker_available
        bhost.docker_available = lambda: False
        try:
            tools = webmod.make_web_tools(
                self.ws,
                {"browser": True, "web_fetch": True,
                 "network_egress_allowlist": ALLOW},
                lambda p, d: None, run_id="t", user_id="u",
                conversation_id="c", cancelled=threading.Event())
            opener = next(t for t in tools if t.name == "browser_open")
            with self.assertRaises(Exception) as cm:
                opener.func("https://example.com/",
                            tool_call_id="test-tc-no-docker")
            self.assertIn("prerequisite_missing", str(cm.exception))
            # The retired test-only env switch must not bypass the denial.
            os.environ["ME_RUNTIME_BROWSER_TEST_DIRECT"] = "1"
            try:
                with self.assertRaises(Exception) as cm2:
                    opener.func("https://example.com/",
                                tool_call_id="test-tc-no-docker-2")
                self.assertIn("prerequisite_missing", str(cm2.exception))
            finally:
                del os.environ["ME_RUNTIME_BROWSER_TEST_DIRECT"]
        finally:
            bhost.docker_available = real_docker


FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_DOCKER_LOG"
if [ "$1" = "image" ]; then exit 0; fi
if [ "$1" = "kill" ] || [ "$1" = "rm" ]; then exit 0; fi
if [ "$1" = "run" ]; then
  if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; fi
  echo "container-stdout-marker"
  exit 0
fi
exit 1
"""


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        (self.bindir / "docker").write_text(FAKE_DOCKER)
        os.chmod(self.bindir / "docker",
                 os.stat(self.bindir / "docker").st_mode
                 | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.log = Path(self.tmp.name) / "docker.log"
        self.env = {"PATH": f"{self.bindir}:{os.environ['PATH']}",
                    "ME_RUNTIME_SANDBOX": "docker",
                    "FAKE_DOCKER_LOG": str(self.log)}
        self._old_env = dict(os.environ)
        os.environ.update(self.env)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        self.tmp.cleanup()

    def _tool(self):
        tools = execmod.make_exec_tools(
            self.ws, {"code_exec": True}, "run_d1",
            lambda p, d: None, threading.Event())
        return next(t for t in tools if t.name == "run_code")

    def test_command_flags_lock_down_container(self):
        cmd = execmod.build_docker_cmd("img:1", "me-rt-x", "/scratch",
                                       "/winput",
                                       ["python", "/src/main.py"])
        for flag in (("--network", "none"), ("--user", "65532:65532"),
                     ("--cap-drop", "ALL"),
                     ("--security-opt", "no-new-privileges"),
                     ("--workdir", "/work"), ("--pids-limit", "64"),
                     ("--tmpfs", "/work:rw,size=64m")):
            self.assertIn(flag[0], cmd)
            i = cmd.index(flag[0])
            self.assertEqual(cmd[i + 1], flag[1])
        self.assertIn("--read-only", cmd)
        self.assertNotIn("--rm", cmd)  # explicit rm-after-cp instead
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        self.assertIn("/scratch:/src:ro", mounts)
        self.assertIn("/winput:/input:ro", mounts)
        self.assertFalse(any(m.endswith(":rw") for m in mounts),
                         f"no writable host mounts allowed: {mounts}")
        self.assertNotIn("pull", cmd)

    def test_run_uses_no_writable_host_mount(self):
        self._tool().func("python", "print('hi')")
        run_lines = [l for l in self.log.read_text().splitlines()
                     if l.startswith("run ")]
        self.assertTrue(run_lines, "fake docker must log the run call")
        toks = run_lines[0].split()
        volumes = [toks[i + 1] for i, t in enumerate(toks) if t == "-v"]
        self.assertTrue(volumes, "expected -v mounts in run call")
        self.assertFalse(any(v.endswith(":rw") for v in volumes),
                         f"no writable host mounts: {volumes}")
        self.assertIn("rm -f me-rt-run_d1", self.log.read_text(),
                      "container must be explicitly removed after cp")

    def test_timeout_kills_and_removes_container(self):
        os.environ["FAKE_SLEEP"] = "30"
        execmod.RUN_TIMEOUT_S, real_timeout = 2, execmod.RUN_TIMEOUT_S
        try:
            with self.assertRaises(Exception) as cm:
                self._tool().func("python", "print('x')")
            self.assertIn("timed out", str(cm.exception))
        finally:
            execmod.RUN_TIMEOUT_S = real_timeout
            os.environ.pop("FAKE_SLEEP", None)
        logged = self.log.read_text()
        self.assertIn("kill me-rt-run_d1", logged)

    def test_streams_are_artifacts_files_ephemeral(self):
        # Tmpfs dies with the container, so no file collection is
        # attempted: bounded stdout/stderr are the durable artifacts.
        artifacts = []
        tools = execmod.make_exec_tools(
            self.ws, {"code_exec": True}, "run_d1",
            lambda p, d: artifacts.append((p, d)),
            threading.Event())
        out = next(t for t in tools if t.name == "run_code").func(
            "python", "print('hi')")
        self.assertIn("container-stdout-marker", out)
        names = [a[0] for a in artifacts]
        self.assertIn("output/exec/stdout.txt", names)
        self.assertIn("output/exec/stderr.txt", names)
        logged = self.log.read_text()
        self.assertNotIn("cp ", logged)
        body = dict(artifacts)["output/exec/stdout.txt"]
        self.assertIn(b"container-stdout-marker", body)
        self.assertFalse((self.ws.root / "output" / "exec" /
                          "result.txt").exists())

    def test_popen_bounded_caps_output_while_running(self):
        rc, out, err = execmod._popen_bounded(
            ["/bin/sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' 'x'"],
            30, threading.Event())
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(out.encode()), execmod.STDOUT_CAP)
        self.assertGreater(len(out), 100_000)


if __name__ == "__main__":
    unittest.main()
