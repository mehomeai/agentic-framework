"""R6 file-broker boundary tests: reserved state + bounded IO (offline).

- `.me_runtime/` (graph.db / pending.json) is denied to ALL public
  file tools, including symlink aliases and normalized paths; listings
  hide it; adversarial attempts leave real resume fixtures unharmed.
- `read_text` streams bounded (1MiB), rejects FIFOs/devices/non-regular
  files, never `read_bytes()`-then-truncate.
- `list_dir` bounds iteration (no `list(iterdir)[:limit]` blowup).
- `files_write` enforces per-file (10MiB) + per-run aggregate caps,
  never silently truncates.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from me_runtime.tools.files import MAX_WRITE_TOTAL_BYTES, make_files_tools
from me_runtime.workspace import (
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    Workspace,
    WorkspaceDenied,
)


def make_ws(tmp, name="task"):
    ws = Path(tmp) / name
    (ws / "input").mkdir(parents=True)
    (ws / "output").mkdir(parents=True)
    return Workspace(str(ws))


def make_tools(ws):
    return make_files_tools(ws, {"files": True}, lambda p, d: None)


def tool_by_name(tools, name):
    return next(t for t in tools if t.name == name)


class ReservedNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)
        # Real resume fixtures that must survive adversarial attempts.
        self.ws.ensure_dirs(".me_runtime")
        (self.ws.root / ".me_runtime" / "pending.json").write_text(
            '{"kind":"waiting_input"}')
        (self.ws.root / ".me_runtime" / "graph.db").write_bytes(b"GRAPHSTATE")
        (self.ws.root / "input" / "notes.md").write_text("hello")
        self.tools = make_tools(self.ws)
        self.read = tool_by_name(self.tools, "files_read")
        self.lst = tool_by_name(self.tools, "files_list")
        self.wr = tool_by_name(self.tools, "files_write")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_reserved_denied(self):
        for p in (".me_runtime/pending.json", ".me_runtime/graph.db",
                  ".me_runtime", ".me_runtime/",
                  "./.me_runtime/pending.json",
                  ".me_runtime/../.me_runtime/graph.db",
                  "output/../../.me_runtime/pending.json"):
            with self.assertRaises(Exception, msg=p) as cm:
                self.read.func(p)
            self.assertIn("workspace_denied", str(cm.exception), p)
        # Fixtures unharmed.
        self.assertEqual(
            (self.ws.root / ".me_runtime" / "pending.json").read_text(),
            '{"kind":"waiting_input"}')
        self.assertEqual(
            (self.ws.root / ".me_runtime" / "graph.db").read_bytes(),
            b"GRAPHSTATE")

    def test_write_reserved_denied_no_harm(self):
        for p in (".me_runtime/evil.md", ".me_runtime/pending.json",
                  ".me_runtime/graph.db",
                  ".me_runtime/../.me_runtime/pwned.txt"):
            with self.assertRaises(Exception, msg=p) as cm:
                self.wr.func(p, "pwned")
            self.assertIn("denied", str(cm.exception), p)
        self.assertFalse((self.ws.root / ".me_runtime" / "evil.md").exists())
        self.assertFalse((self.ws.root / ".me_runtime" / "pwned.txt").exists())
        self.assertEqual(
            (self.ws.root / ".me_runtime" / "pending.json").read_text(),
            '{"kind":"waiting_input"}')

    def test_list_reserved_denied_and_hidden(self):
        with self.assertRaises(Exception) as cm:
            self.lst.func(".me_runtime")
        self.assertIn("workspace_denied", str(cm.exception))
        top = self.lst.func(".")
        self.assertNotIn(".me_runtime", top)
        self.assertNotIn(".me_runtime/pending.json", top)
        self.assertIn("input", top)

    def test_symlink_alias_into_reserved_denied(self):
        link = self.ws.root / "input" / "link_to_state"
        try:
            link.symlink_to(self.ws.root / ".me_runtime")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(Exception) as cm:
            self.read.func("input/link_to_state/pending.json")
        self.assertIn("workspace_denied", str(cm.exception))
        with self.assertRaises(Exception) as cm:
            self.lst.func("input/link_to_state")
        self.assertIn("workspace_denied", str(cm.exception))
        with self.assertRaises(Exception) as cm:
            self.wr.func("input/link_to_state/pwned.txt", "x")
        self.assertIn("denied", str(cm.exception))
        # Real state unharmed; alias did not create inside reserved.
        self.assertFalse(
            (self.ws.root / ".me_runtime" / "pwned.txt").exists())
        self.assertEqual(
            (self.ws.root / ".me_runtime" / "pending.json").read_text(),
            '{"kind":"waiting_input"}')

    def test_symlink_parent_write_denied(self):
        # output/link -> .me_runtime : writes through it must land denied.
        link = self.ws.root / "output" / "link"
        try:
            link.symlink_to(self.ws.root / ".me_runtime")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(Exception):
            self.wr.func("output/link/pwned2.txt", "x")
        self.assertFalse(
            (self.ws.root / ".me_runtime" / "pwned2.txt").exists())

    def test_internal_api_still_reaches_state(self):
        # Internal runtime paths (resolve/ensure_dirs/root) are unaffected.
        p = self.ws.resolve(".me_runtime/pending.json")
        self.assertTrue(p.is_file())
        self.ws.ensure_dirs(".me_runtime")
        self.assertTrue((self.ws.root / ".me_runtime").is_dir())


class BoundedIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self.tmp.name)
        self.tools = make_tools(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_read_bounded_no_oom(self):
        big = self.ws.root / "input" / "big.bin"
        big.write_bytes(b"A" * (2_000_000))
        out = tool_by_name(self.tools, "files_read").func("input/big.bin")
        self.assertLessEqual(len(out.encode()), MAX_READ_BYTES + 16)

    def test_fifo_and_directory_rejected(self):
        fifo = self.ws.root / "input" / "pipe"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):
            self.skipTest("fifo unavailable")
        with self.assertRaises(Exception) as cm:
            tool_by_name(self.tools, "files_read").func("input/pipe")
        self.assertIn("workspace_denied", str(cm.exception))
        with self.assertRaises(Exception):
            tool_by_name(self.tools, "files_read").func("input")

    def test_list_bounds_iteration(self):
        d = self.ws.root / "input" / "many"
        d.mkdir()
        for i in range(300):
            (d / f"f{i:03d}.txt").write_text("x")
        items = self.ws.list_dir("input/many", limit=200)
        self.assertLessEqual(len(items), 200)

    def test_write_caps_never_truncate(self):
        wr = tool_by_name(self.tools, "files_write")
        # Per-file cap refuses instead of truncating.
        with self.assertRaises(Exception) as cm:
            wr.func("output/huge.txt", "x" * (MAX_WRITE_BYTES + 1))
        self.assertIn("too large", str(cm.exception))
        self.assertFalse((self.ws.root / "output" / "huge.txt").exists())
        # Aggregate quota refuses instead of truncating.
        import me_runtime.tools.files as fmod
        old_total = fmod.MAX_WRITE_TOTAL_BYTES
        fmod.MAX_WRITE_TOTAL_BYTES = 100
        try:
            tools = make_files_tools(self.ws, {"files": True},
                                     lambda p, d: None)
            w2 = tool_by_name(tools, "files_write")
            w2.func("output/a.txt", "x" * 60)
            with self.assertRaises(Exception) as cm2:
                w2.func("output/b.txt", "x" * 60)
            self.assertIn("quota", str(cm2.exception))
            # First write intact, second refused (not partial).
            self.assertEqual(
                (self.ws.root / "output" / "a.txt").read_text(), "x" * 60)
            self.assertFalse((self.ws.root / "output" / "b.txt").exists())
        finally:
            fmod.MAX_WRITE_TOTAL_BYTES = old_total
        self.assertEqual(MAX_WRITE_TOTAL_BYTES, 20_000_000)

    def test_strict_path_types(self):
        with self.assertRaises(Exception) as cm:
            tool_by_name(self.tools, "files_read").func(123)  # type: ignore
        self.assertIn("invalid_request", str(cm.exception))


class BridgeBoolTests(unittest.TestCase):
    def test_non_bool_capability_rejected(self):
        from me_runtime.bridge import _validate_request
        base = {"bridge": "runtime-bridge-v1", "op": "run",
                "run_id": "r", "user_id": "u", "conversation_id": "c",
                "workspace": "/tmp", "request": "hi",
                "capabilities": {"files": "true"},
                "model": {"provider": "fixture", "model": "x"}}
        with self.assertRaises(ValueError) as cm:
            _validate_request(base)
        self.assertIn("must be a boolean", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
