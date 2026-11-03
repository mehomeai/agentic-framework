"""R9 strict serde: unsafe constructors blocked, legacy checkpoint resumes."""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"


@dataclasses.dataclass
class _Evil:
    cmd: str = "id"


class StrictSerdeTests(unittest.TestCase):
    def test_unsafe_constructor_not_executed(self):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        permissive = JsonPlusSerializer(pickle_fallback=False,
                                        allowed_msgpack_modules=True)
        strict = JsonPlusSerializer(pickle_fallback=False,
                                    allowed_msgpack_modules=None)
        data = permissive.dumps_typed(_Evil())
        # Permissive reconstructs the type.
        got_permissive = permissive.loads_typed(data)
        self.assertIsInstance(got_permissive, _Evil)
        # Strict refuses the constructor: returns inert data, never instance.
        got_strict = strict.loads_typed(data)
        self.assertNotIsInstance(got_strict, _Evil)
        # No pickle fallback anywhere.
        self.assertFalse(strict.pickle_fallback)
        self.assertFalse(permissive.pickle_fallback)

    def test_bridge_defaults_strict_env(self):
        import subprocess, json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "task"
            (ws / "input").mkdir(parents=True)
            req = {
                "bridge": "runtime-bridge-v1", "op": "run",
                "run_id": "run_strict_env", "user_id": "alice",
                "conversation_id": "conv_a", "workspace": str(ws),
                "request": "hello",
                "capabilities": {"files": True},
                "model": {"provider": "fixture", "model": "fixture-echo"},
                "budgets": {"max_steps": 2, "max_wall_seconds": 20,
                            "max_tool_calls": 5},
            }
            import os
            env = dict(os.environ)
            env.pop("LANGGRAPH_STRICT_MSGPACK", None)
            env.pop("ME_RUNTIME_SANDBOX", None)
            proc = subprocess.run(
                [str(PY), "-c",
                 "import os, subprocess, json; "
                 "import me_runtime.bridge as b; "
                 "print(os.environ.get('LANGGRAPH_STRICT_MSGPACK')); "
                 "from langgraph.checkpoint.serde import _msgpack as m; "
                 "print(m.STRICT_MSGPACK_ENABLED); "
                 "print(b._strict_serde()._allowed_msgpack_modules)"],
                capture_output=True, text=True, cwd=str(REPO), env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            # bridge import must default env to true even when unset.
            self.assertIn("true", lines[0].lower())
            self.assertEqual(lines[1].strip(), "True")
            # Explicit strict serializer: None means allowlist-only.
            self.assertIn("None", lines[2])

    def test_legacy_06_trusted_checkpoint_resumes(self):
        fixture = REPO / "tests" / "fixtures" / "legacy_0_6_paused_ask"
        self.assertTrue((fixture / "graph.db").exists())
        self.assertTrue((fixture / "pending.json").exists())
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "task1"
            (ws / "input").mkdir(parents=True)
            (ws / ".me_runtime").mkdir(parents=True)
            (ws / "output").mkdir(parents=True)
            shutil.copy(fixture / "graph.db",
                        ws / ".me_runtime" / "graph.db")
            pend = json.loads((fixture / "pending.json").read_text())
            effect_expected = (fixture / "legacy_effect.md").read_text()
            # Patch workspace-bound pending to temp dir; keep legacy shape
            # (wall_start only, no active_wall_seconds) to prove migration.
            pend.pop("active_wall_seconds", None)
            pend["workspace"] = str(ws)
            pend["run_id"] = "run_legacy_1"
            pend["user_id"] = "alice"
            pend["conversation_id"] = "conv_a"
            (ws / ".me_runtime" / "pending.json").write_text(
                json.dumps(pend, indent=2))
            (ws / "output" / "legacy_effect.md").write_text(effect_expected)
            before_mtime = (ws / "output" / "legacy_effect.md").stat().st_mtime_ns
            req = {
                "bridge": "runtime-bridge-v1", "op": "resume",
                "run_id": "run_legacy_1", "user_id": "alice",
                "conversation_id": "conv_a", "workspace": str(ws),
                "request": pend["request"],
                "inputs": [{"id": "q1", "answer": "Friday"}],
                "capabilities": {"files": True, "web_fetch": False,
                                 "browser": False, "code_exec": False,
                                 "animation": False,
                                 "network_egress_allowlist": []},
                "model": {"provider": "fixture", "model": "fixture-echo"},
                "budgets": {"max_steps": 8, "max_wall_seconds": 60,
                            "max_tool_calls": 20},
            }
            import os
            env = dict(os.environ)
            env.pop("ME_RUNTIME_SANDBOX", None)
            proc = subprocess.run(
                [str(PY), "-m", "me_runtime.bridge"],
                input=json.dumps(req), capture_output=True, text=True,
                timeout=60, cwd=str(REPO), env=env)
            events = [json.loads(l) for l in proc.stdout.splitlines()
                      if l.strip()]
            term = next(e for e in events if e.get("event") == "result")
            self.assertEqual(term["status"], "completed", term)
            self.assertIn("Friday", term["output"])
            # Existing completed write effect unchanged (no replay).
            self.assertEqual((ws / "output" / "legacy_effect.md").read_text(),
                             effect_expected)
            self.assertEqual((ws / "output" / "legacy_effect.md").stat().st_mtime_ns,
                             before_mtime)


if __name__ == "__main__":
    unittest.main()
