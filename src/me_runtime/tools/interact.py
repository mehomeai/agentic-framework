"""Human-in-the-loop tools on real harness interrupt semantics.

`ask_user` pauses the LangGraph run with a `waiting_input` payload;
`request_approval` pauses it with an `awaiting_approval` payload. Both
resume through the persisted SqliteSaver checkpoint via Command(resume=...),
so already-completed tool effects are never replayed. Decisions are bound
to the exact pending action/args hash — verified here AND in the bridge.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.tools import tool
from langgraph.types import interrupt

from ..errors import ToolDeniedError


def args_hash(args: dict) -> str:
    canonical = json.dumps(args or {}, sort_keys=True,
                           ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_interact_tools(run_id: str) -> list[Any]:
    @tool("ask_user")
    def ask_user(id: str, prompt: str) -> str:
        """Ask the user a question; resumes with their answer. Args: id, prompt."""
        payload = {"kind": "waiting_input", "id": id, "prompt": prompt,
                   "run_id": run_id}
        resume = interrupt(payload)
        if not isinstance(resume, dict) or not resume.get("answer"):
            raise ToolDeniedError("invalid_request",
                                  "resume missing answer for "
                                  f"{id!r}")
        return f"user answered: {resume['answer']}"[:2000]

    @tool("request_approval")
    def request_approval(id: str, action: str, params: dict | None = None,
                         reason: str = "") -> str:
        """Propose a gated action; resumes with the decision. Args: id, action, params, reason."""
        params = params or {}
        payload = {"kind": "awaiting_approval", "id": id, "action": action,
                   "args": params, "args_hash": args_hash(params),
                   "reason": reason, "run_id": run_id}
        resume = interrupt(payload)
        if not isinstance(resume, dict):
            raise ToolDeniedError("invalid_request",
                                  "resume missing decision")
        if resume.get("args_hash") != payload["args_hash"]:
            raise ToolDeniedError("invalid_request",
                                  "approval args changed since proposal")
        if resume.get("decision") != "approve":
            raise ToolDeniedError("approval_rejected",
                                  f"approval {id!r} rejected by user")
        return f"approved: {action}"

    return [ask_user, request_approval]
