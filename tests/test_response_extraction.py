"""Structured provider content-block extraction (offline regression).

Modern chat adapters (Responses / Anthropic styles) return AIMessage
content as LISTS of blocks, not plain str. The bridge previously read
only str finals, silently discarding the real answer and emitting "done".
These tests prove extraction through the real harness path:

- unit: _message_text / _final_text over str, text blocks, Responses
  output_text blocks, reasoning/refusal/image/tool exclusion, empty
  tool-call messages, pure-final preference, RUN-FAILED list parsing;
- bridge: fixture finals emitted as real content-block lists through the
  actual bridge subprocess assert result output + artifact bytes.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from me_runtime.bridge import (_final_text, _message_text, _parse_run_failed)

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"


def tool_call_msg(text=""):
    return AIMessage(
        content=text,
        tool_calls=[{"name": "delegate_task", "args": {},
                     "id": "tc1", "type": "tool_call"}])


class MessageTextTests(unittest.TestCase):
    def test_str_passthrough(self):
        self.assertEqual(_message_text(AIMessage(content="hello")), "hello")

    def test_text_blocks_joined(self):
        m = AIMessage(content=[{"type": "text", "text": "ORB"},
                               {"type": "text", "text": "IT"}])
        self.assertEqual(_message_text(m), "ORBIT")

    def test_responses_output_text_extracted(self):
        m = AIMessage(content=[{"type": "output_text",
                                "text": "Child wrote ORBIT, in English."}])
        self.assertIn("ORBIT", _message_text(m))

    def test_reasoning_refusal_image_tool_excluded(self):
        m = AIMessage(content=[
            {"type": "reasoning", "reasoning": "secret-plan"},
            {"type": "refusal", "refusal": "no"},
            {"type": "image", "source_type": "url", "url": "http://x/y.png"},
            {"type": "tool_call", "id": "1", "name": "x", "args": {}},
            {"type": "text", "text": "visible"}])
        text = _message_text(m)
        self.assertIn("visible", text)
        self.assertNotIn("secret-plan", text)

    def test_empty_and_toolcall_only(self):
        self.assertEqual(_message_text(AIMessage(content=[])), "")
        self.assertEqual(_message_text(tool_call_msg()), "")


class FinalTextTests(unittest.TestCase):
    def test_prefers_newest_pure_final(self):
        older = AIMessage(content="older answer")
        latest = AIMessage(content=[{"type": "output_text",
                                     "text": "Child wrote ORBIT, in English."}])
        self.assertIn("ORBIT", _final_text([older, latest]))

    def test_skips_narration_beside_tool_calls(self):
        narrated = tool_call_msg("working on it")
        final = AIMessage(content="final ORBIT answer")
        self.assertEqual(_final_text([narrated, final]), "final ORBIT answer")

    def test_falls_back_to_narration_only_if_no_pure_final(self):
        narrated = tool_call_msg("working on it")
        self.assertEqual(_final_text([narrated]), "working on it")

    def test_run_failed_in_blocks_still_parsed(self):
        m = AIMessage(content=[{"type": "text",
                                "text": "RUN-FAILED tool_error: boom"}])
        parsed = _parse_run_failed(_final_text([m]))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "tool_error")


def run_bridge(request_text: str):
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "task"
        (ws / "input").mkdir(parents=True)
        req = {
            "bridge": "runtime-bridge-v1", "op": "run",
            "run_id": "run_struct_1", "user_id": "synthetic",
            "conversation_id": "conv_synth", "workspace": str(ws),
            "request": request_text,
            "capabilities": {"files": True},
            "model": {"provider": "fixture", "model": "fixture-echo"},
            "budgets": {"max_steps": 4, "max_wall_seconds": 60,
                        "max_tool_calls": 5},
        }
        env = dict(os.environ)
        env.pop("ME_HOME_TASK_RPC_FD", None)
        proc = subprocess.run(
            [str(PY), "-m", "me_runtime.bridge"],
            input=json.dumps(req), capture_output=True, text=True,
            timeout=120, cwd=str(REPO), env=env)
        events = [json.loads(l) for l in proc.stdout.splitlines()
                  if l.strip()]
        term = next(e for e in events if e.get("event") == "result")
        arts = {a["path"]: a for a in (term.get("artifacts") or [])}
        result_md = ws / "output" / "result.md"
        artifact_bytes = result_md.read_bytes() if result_md.exists() else b""
        return term, arts, artifact_bytes


class BridgeStructuredTests(unittest.TestCase):
    def test_structured_text_blocks_end_to_end(self):
        term, _arts, blob = run_bridge(
            "STRUCTURED:Child wrote ORBIT, reporting in English.")
        self.assertEqual(term["status"], "completed", term)
        self.assertIn("ORBIT", term["output"])
        self.assertIn("English", term["output"])
        self.assertIn(b"ORBIT", blob)

    def test_structured_responses_blocks_end_to_end(self):
        term, _arts, blob = run_bridge(
            "STRUCTURED_RESP:Child wrote ORBIT, reporting in English.")
        self.assertEqual(term["status"], "completed", term)
        self.assertIn("ORBIT", term["output"])
        self.assertIn("English", term["output"])
        self.assertIn(b"ORBIT", blob)

    def test_structured_mixed_excludes_reasoning(self):
        term, _arts, blob = run_bridge(
            "STRUCTURED_MIXED:visible ORBIT English")
        self.assertEqual(term["status"], "completed", term)
        self.assertIn("ORBIT", term["output"])
        self.assertNotIn("secret-must-not-leak", term["output"])
        self.assertNotIn(b"secret-must-not-leak", blob)


if __name__ == "__main__":
    unittest.main()
