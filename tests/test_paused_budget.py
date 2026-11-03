"""R9 paused-budget: active wall, not paused calendar wait."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"


def base_request(ws: Path, **over):
    req = {
        "bridge": "runtime-bridge-v1",
        "op": "run",
        "run_id": "run_budget_1",
        "user_id": "alice",
        "conversation_id": "conv_a",
        "workspace": str(ws),
        "request": "WRITE_THEN_ASK: what is the deadline?",
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


def run_bridge(req: dict):
    env = dict(__import__("os").environ)
    env.pop("ME_RUNTIME_SANDBOX", None)
    proc = subprocess.run(
        [str(PY), "-m", "me_runtime.bridge"],
        input=json.dumps(req), capture_output=True, text=True,
        timeout=60, cwd=str(REPO), env=env)
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    term = next(e for e in events if e.get("event") == "result")
    return proc, term, events


class PausedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "task1"
        (self.ws / "input").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _park(self):
        proc, term, _ = run_bridge(base_request(self.ws))
        self.assertEqual(term["status"], "waiting_input", term)
        pend_path = self.ws / ".me_runtime" / "pending.json"
        self.assertTrue(pend_path.exists())
        effect = (self.ws / "output" / "legacy_effect.md").read_text()
        mtime = (self.ws / "output" / "legacy_effect.md").stat().st_mtime_ns
        return pend_path, effect, mtime

    def test_aged_pending_resumes_small_prior_active(self):
        """24h paused calendar wait must not burn 60s active budget."""
        pend_path, effect_before, mtime_before = self._park()
        pend = json.loads(pend_path.read_text())
        # Age calendar 24h, keep active small.
        pend["wall_start"] = time.time() - 24 * 3600
        pend["active_wall_seconds"] = 1.0
        pend_path.write_text(json.dumps(pend, indent=2))
        resume = base_request(
            self.ws, op="resume",
            inputs=[{"id": "q1", "answer": "Friday"}])
        _p, term, _e = run_bridge(resume)
        self.assertEqual(term["status"], "completed", term)
        self.assertIn("Friday", term["output"])
        # Prior completed write effect unchanged (no replay).
        self.assertEqual((self.ws / "output" / "legacy_effect.md").read_text(),
                         effect_before)
        self.assertEqual((self.ws / "output" / "legacy_effect.md").stat().st_mtime_ns,
                         mtime_before)

    def test_accumulated_active_over_budget_fails_before_effects(self):
        pend_path, effect_before, mtime_before = self._park()
        pend = json.loads(pend_path.read_text())
        pend["wall_start"] = time.time() - 24 * 3600
        pend["active_wall_seconds"] = 61.0  # wall is 60
        pend_path.write_text(json.dumps(pend, indent=2))
        before = (self.ws / "output" / "legacy_effect.md").stat().st_mtime_ns
        resume = base_request(
            self.ws, op="resume",
            inputs=[{"id": "q1", "answer": "Friday"}])
        _p, term, _e = run_bridge(resume)
        self.assertEqual(term["status"], "failed", term)
        self.assertEqual(term["error"]["code"], "budget_exhausted")
        self.assertEqual((self.ws / "output" / "legacy_effect.md").stat().st_mtime_ns,
                         before)

    def test_legacy_wall_start_migrates_and_resumes(self):
        """Old checkpoint with only wall_start (no active) must resume."""
        pend_path, effect_before, _m = self._park()
        pend = json.loads(pend_path.read_text())
        # Simulate legacy 0.6: drop active, age wall_start 24h.
        pend.pop("active_wall_seconds", None)
        pend["wall_start"] = time.time() - 24 * 3600
        pend_path.write_text(json.dumps(pend, indent=2))
        resume = base_request(
            self.ws, op="resume",
            inputs=[{"id": "q1", "answer": "Friday"}])
        _p, term, _e = run_bridge(resume)
        self.assertEqual(term["status"], "completed", term)
        self.assertIn("Friday", term["output"])
        self.assertEqual((self.ws / "output" / "legacy_effect.md").read_text(),
                         effect_before)


if __name__ == "__main__":
    unittest.main()
