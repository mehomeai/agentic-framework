"""Agent loop owned by the maintained LangGraph prebuilt ReAct harness.

`build_agent` wires exactly the brokered Me tools — no framework default
filesystem/shell/subagent tools — into `create_react_agent`. The model
(either a maintained chat adapter or the test-only fixture stub) drives
standard tool-call round trips; this module contains no custom planner,
no manual /chat/completions parsing, and no parallel task scheduler.

R9: `create_react_agent` is deprecated upstream since 1.0 in favour of
`langchain.agents.create_agent` (LangGraphDeprecatedSinceV10, to be removed
in V2). The alias remains supported in the pinned 1.x line and preserves
0.6 checkpoint resume compat, so it is kept here with truthful docs; the
ToolNode is constructed explicitly with handle_tool_errors=True to restore
0.6 semantics where broker denials (ToolDeniedError) become error
ToolMessages for the model instead of aborting the run (1.x default only
handles ToolInvocationError).
"""
from __future__ import annotations

import threading
from typing import Any

from langgraph.prebuilt import create_react_agent

from .models import FixtureChatModel
from .tools.animate import make_animate_tools
from .tools.delegate import make_delegate_tools
from .tools.exec import make_exec_tools
from .tools.files import make_files_tools
from .tools.interact import make_interact_tools
from .tools.web import make_web_tools
from .workspace import Workspace

SYSTEM_PROMPT = """You are Me, a home agent. You act ONLY through the tools
provided by the home: scoped files, gated web fetch/browser, sandboxed
code execution, animation building, and explicit user questions/approvals.
Rules:
- Never claim to have done work you did not do via tools; never fabricate
  file contents, fetch results, renders, or approvals.
- Stay inside the granted capabilities. If a tool reports a denial or
  error you cannot recover from, end your reply with exactly:
  RUN-FAILED <code>: <message>
  using the code from the tool error.
- Keep replies short; the home delivers your final text and artifacts.
"""

EXPECTED_TOOL_NAMES = sorted([
    "files_read", "files_list", "files_write",
    "web_fetch", "browser_open",
    "run_code",
    "animation_build",
    "ask_user", "request_approval",
    "delegate_task", "list_children", "get_child", "await_children",
])


class RunTools:
    """Per-run broker state shared by tool closures (no global state)."""

    def __init__(self, run_id: str, user_id: str, conversation_id: str,
                 cancelled: threading.Event,
                 task_rpc=None, capabilities: dict | None = None,
                 budgets: dict | None = None) -> None:
        self.run_id = run_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.cancelled = cancelled
        self.artifacts: list[dict] = []
        self.task_rpc = task_rpc
        self.capabilities = dict(capabilities or {})
        self.budgets = dict(budgets or {})

    def record_artifact(self, path: str, data: bytes) -> None:
        import hashlib
        self.artifacts.append({
            "path": path, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()})


def build_tools(ws: Workspace, capabilities: dict, ctx: RunTools
                ) -> list[Any]:
    tools = []
    tools += make_files_tools(ws, capabilities, ctx.record_artifact)
    tools += make_web_tools(ws, capabilities, ctx.record_artifact,
                            ctx.run_id, ctx.user_id, ctx.conversation_id,
                            ctx.cancelled)
    tools += make_exec_tools(ws, capabilities, ctx.run_id,
                             ctx.record_artifact, ctx.cancelled)
    tools += make_animate_tools(ws, capabilities, ctx.record_artifact)
    tools += make_interact_tools(ctx.run_id)
    tools += make_delegate_tools(ctx, capabilities, ctx.budgets)
    names = sorted(t.name for t in tools)
    assert names == EXPECTED_TOOL_NAMES, f"tool allowlist drift: {names}"
    return tools


def build_agent(model, tools: list[Any], checkpointer=None):
    """Compile the maintained prebuilt ReAct agent (harness-owned loop)."""
    from langgraph.prebuilt import ToolNode
    scoped_prompt = (f"{SYSTEM_PROMPT}\n"
                     f"[run context: user and conversation are labelled by "
                     f"the home; only workspace files and supplied context "
                     f"are visible]")
    # Explicit ToolNode restores 0.6 tool-error semantics (denials as error
    # ToolMessages) on LangGraph 1.x; create_react_agent accepts ToolNode.
    tool_node = ToolNode(tools, handle_tool_errors=True)
    return create_react_agent(model, tool_node, prompt=scoped_prompt,
                              checkpointer=checkpointer)


def is_fixture_model(model) -> bool:
    return isinstance(model, FixtureChatModel)
