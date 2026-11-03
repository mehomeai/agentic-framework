"""Opt-in live-model / backend checks — SKIPPED unless explicitly enabled.

These never run in CI and never touch household data (synthetic prompts
only). Distinguished from deterministic fixture tests:

  Local Ollama (no key; needs a running Ollama on the home host):
    ME_RUNTIME_LIVE_TESTS=1 ME_RUNTIME_LIVE_PROVIDER=local \\
      ME_RUNTIME_LIVE_MODEL=llama3.1 \\
      .venv/bin/python -m pytest tests/test_live_optin.py -v -k ollama

  Remote OpenAI-compatible incl. Responses API (explicit opt-in + key):
    ME_RUNTIME_LIVE_TESTS=1 ME_RUNTIME_ALLOW_REMOTE=1 \\
      ME_RUNTIME_MODEL_API_KEY="..." \\
      ME_RUNTIME_LIVE_PROVIDER=openai ME_RUNTIME_LIVE_MODEL=gpt-4o-mini \\
      ME_RUNTIME_LIVE_ENDPOINT=https://api.openai.com/v1 \\
      ME_RUNTIME_LIVE_RESPONSES=1 \\
      .venv/bin/python -m pytest tests/test_live_optin.py -v -k remote

  Honest backend presence report (passes either way; asserts only that the
  report matches reality):
    ME_RUNTIME_LIVE_TESTS=1 .venv/bin/python -m pytest \\
      tests/test_live_optin.py -v -k backends
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"
LIVE = os.environ.get("ME_RUNTIME_LIVE_TESTS") == "1"


def run_live(model: dict, prompt: str):
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "task"
        (ws / "input").mkdir(parents=True)
        req = {
            "bridge": "runtime-bridge-v1", "op": "run",
            "run_id": "run_live_1", "user_id": "synthetic",
            "conversation_id": "conv_synth", "workspace": str(ws),
            "request": prompt,
            "capabilities": {"files": True},
            "model": model,
            "budgets": {"max_steps": 4, "max_wall_seconds": 180,
                        "max_tool_calls": 5},
        }
        env = dict(os.environ)
        proc = subprocess.run(
            [str(PY), "-m", "me_runtime.bridge"],
            input=json.dumps(req), capture_output=True, text=True,
            timeout=200, cwd=str(REPO), env=env)
        events = [json.loads(l) for l in proc.stdout.splitlines()
                  if l.strip()]
        term = next(e for e in events if e.get("event") == "result")
        return proc, term


@unittest.skipUnless(LIVE, "opt-in live tests disabled "
                           "(set ME_RUNTIME_LIVE_TESTS=1)")
class LiveOptInTests(unittest.TestCase):
    def test_live_ollama_synthetic(self):
        """Synthetic prompt through local Ollama (langchain-ollama)."""
        if os.environ.get("ME_RUNTIME_LIVE_PROVIDER") != "local":
            self.skipTest("set ME_RUNTIME_LIVE_PROVIDER=local")
        model = os.environ.get("ME_RUNTIME_LIVE_MODEL", "")
        if not model:
            self.skipTest("set ME_RUNTIME_LIVE_MODEL")
        _p, term = run_live(
            {"provider": "local", "model": model},
            "Reply with the single word: pineapple. No other text.")
        print("LIVE-OLLAMA status:", term.get("status"))
        if term.get("status") == "failed" and term.get("error", {}).get(
                "code") == "model_not_configured":
            self.skipTest(f"ollama unreachable: {term['error']}")
        self.assertEqual(term["status"], "completed")
        self.assertIn("pineapple", term["output"].lower())

    def test_live_remote_synthetic(self):
        """Synthetic prompt through an explicitly opted-in remote provider."""
        if os.environ.get("ME_RUNTIME_LIVE_PROVIDER") != "openai":
            self.skipTest("set ME_RUNTIME_LIVE_PROVIDER=openai")
        for var in ("ME_RUNTIME_ALLOW_REMOTE", "ME_RUNTIME_MODEL_API_KEY",
                    "ME_RUNTIME_LIVE_MODEL"):
            if not os.environ.get(var):
                self.skipTest(f"set {var}")
        model = {"provider": "openai",
                 "model": os.environ["ME_RUNTIME_LIVE_MODEL"],
                 "api_key_env": "ME_RUNTIME_MODEL_API_KEY"}
        if os.environ.get("ME_RUNTIME_LIVE_ENDPOINT"):
            model["endpoint"] = os.environ["ME_RUNTIME_LIVE_ENDPOINT"]
        if os.environ.get("ME_RUNTIME_LIVE_RESPONSES") == "1":
            model["use_responses_api"] = True
        _p, term = run_live(
            model, "Reply with the single word: pineapple. No other text.")
        print("LIVE-REMOTE status:", term.get("status"),
              "model:", term.get("model"))
        self.assertEqual(term["status"], "completed")
        self.assertIn("pineapple", term["output"].lower())

    def test_backend_presence_report(self):
        """Honest backend report: asserts the report matches reality."""
        from me_runtime import prereqs
        info = prereqs.report()
        print("prereqs:", info)
        self.assertEqual(info["ffmpeg"], bool(shutil.which("ffmpeg")))
        self.assertEqual(info["docker"], bool(shutil.which("docker")))
        self.assertEqual(info["pillow"], True)  # installed in R2 venv
        self.assertEqual(info["harness"], "langgraph-prebuilt-react")


if __name__ == "__main__":
    unittest.main()
