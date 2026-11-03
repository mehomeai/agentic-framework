"""TEST-SUPPORT harness adapter (devmode component tests only; NOT shipped).

Lives under tests/support_upstream so it is never part of the production
package. Not the production path. Production is the full OpenBot platform: the
native `agent-langgraph-agui` container serves AG-UI, the OpenBot server
owns `/api/agent-tools/call`, and home-owned self-hosted Intelligence
persists threads (see `results/runtime-wire-contract.json`). This module
exists so component tests exercise the REAL vendored upstream classes
(`upstream/vendor/tool_runtime.py`, `upstream/vendor/parallel_tools.py`
via `ag-ui-langgraph==0.0.45`) with a controlled model and a loopback
tool gateway — never as a second production engine, never with a
second pending graph or job authority.

Graph topology, tool ownership, and callback protocol are the vendored
upstream modules. Two reviewable compat patches vs upstream `main.py`
(see `upstream/vendor/VENDOR.md`):
  PATCH-D1: SqliteSaver (strict, no pickle) instead of MemorySaver,
    so devmode restarts survive the process.
  PATCH-D2: host-selected maintained model hook instead of `_model()`.

Surface tools (`ask_user`, `request_approval`, `await_children`) are NOT
in the deployment set, so vendored `next_step` ENDs the run for the
surface — no LangGraph `interrupt()` from an unrelated HTTP thread.
Resume appends trusted ToolMessages to the SAME SQLite thread and
re-invokes; completed deployment calls are checkpointed, never replayed.
"""
from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph

from me_runtime.upstream.vendor.parallel_tools import ParallelToolAgent  # noqa: F401 (vendored class in MRO)
from me_runtime.upstream.vendor.tool_runtime import (
    ToolAwareAgent,
    bind_tools,
    execute_tools,
    model_messages,
    next_step,
)

SURFACE_TOOLS = frozenset({"ask_user", "request_approval", "await_children"})

ENGINE_ID = "openbot-langgraph-agui"


def split_tools(all_names: list[str]) -> frozenset[str]:
    """Deployment-owned names: everything except the surface set."""
    return frozenset(n for n in all_names if n not in SURFACE_TOOLS)


def agui_tools_from_lc(lc_tools: list[Any]) -> list[Any]:
    """Convert LangChain tools to AG-UI Tool list (name/description/schema)."""
    from ag_ui.core import Tool as AGUITool

    out = []
    for t in lc_tools:
        try:
            schema = t.tool_call_schema.model_json_schema()
        except Exception:  # noqa: BLE001
            try:
                props = dict(t.args or {})
                schema = {"type": "object", "properties": props}
            except Exception:  # noqa: BLE001
                schema = {"type": "object", "properties": {}}
        # Strip non-JSON-schema keys langchain adds (e.g. $defs handling
        # stays; pydantic dumps are JSON-serializable by construction).
        out.append(AGUITool(name=t.name,
                            description=t.description or t.name,
                            parameters=schema))
    return out


def build_upstream_graph(model: Any, checkpointer: Any = None):
    """Compile the upstream StateGraph topology with Me model + checkpointer.

    Mirrors `upstream/vendor/main.py`: answer → tools → answer with
    vendored `next_step` edge. `answer` mirrors upstream logic
    (`model_messages`, Anthropic system-turns-first, typed provider-auth
    → `authentication_failed`) with the injected maintained model.
    """
    from me_runtime.upstream.vendor.tool_runtime import current_tools

    async def answer(state: MessagesState):
        from langchain_anthropic import ChatAnthropic  # noqa: PLC0415 (lazy: only needed for Anthropic)

        try:
            messages = model_messages(state["messages"])
            if isinstance(model, ChatAnthropic):
                from langchain_core.messages import (
                    SystemMessage as _SM,
                )
                from langchain_core.messages import convert_to_messages
                conv = convert_to_messages(messages)
                messages = [*(m for m in conv if isinstance(m, _SM)),
                            *(m for m in conv if not isinstance(m, _SM))]
            return {"messages": [await bind_tools(model).ainvoke(messages)]}
        except Exception as exc:  # noqa: BLE001 (typed boundary only)
            name = type(exc).__name__
            mod = type(exc).__module__
            if ("AuthenticationError" in name or "PermissionDeniedError" in name
                    or "RefreshError" in name or "auth" in mod.lower()):
                try:
                    current_tools().connection["authentication_failed"] = True
                except Exception:  # noqa: BLE001
                    pass
            raise

    builder = StateGraph(MessagesState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_node("tools", execute_tools)
    builder.add_conditional_edges("answer", next_step)
    builder.add_edge("tools", "answer")
    return builder.compile(checkpointer=checkpointer)


def new_agent(graph: Any, recursion_limit: int = 25) -> ToolAwareAgent:
    """Wrap the compiled graph in the vendored ToolAwareAgent."""
    return ToolAwareAgent(name="openbot", graph=graph,
                          config={"recursion_limit": recursion_limit})


def run_input(thread_id: str, run_id: str, lc_tools: list[Any],
              deployment: frozenset[str], assertion: str,
              human_text: str, history: list | None = None,
              context: list | None = None) -> Any:
    """Build the AG-UI RunAgentInput for one attempt (no secrets inside)."""
    from ag_ui.core import RunAgentInput

    import uuid as _uuid
    tools = agui_tools_from_lc(lc_tools)
    messages: list[dict] = []
    for m in (history or []):
        messages.append(m)
    messages.append({"id": f"user-{_uuid.uuid4().hex[:8]}",
                     "role": "user", "content": human_text})
    # Exact upstream wire shape (agent-langgraph-agui/tests/test_tool_protocol
    # `run_input`): messages carry ids, top-level `state` present.
    return RunAgentInput(threadId=thread_id, runId=run_id, messages=messages,  # type: ignore[arg-type]
                         tools=tools, context=context or [], state={},
                         forwardedProps={"openbotRun": assertion,
                                         "openbotDeploymentTools": sorted(deployment)})


class tool_callback_env:
    """Point vendored `execute_tools` at a gateway for one attempt.

    Sets `OPENBOT_TOOL_URL`/`AGENT_TOOL_TOKEN` for the enclosed block and
    restores the previous environment after. Never logs the token.
    """

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> "tool_callback_env":
        for k in ("OPENBOT_TOOL_URL", "AGENT_TOOL_TOKEN"):
            self._old[k] = os.environ.get(k)
        os.environ["OPENBOT_TOOL_URL"] = self.url + "/api/agent-tools/call"
        os.environ["AGENT_TOOL_TOKEN"] = self.token
        return self

    def __exit__(self, *exc: Any) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _strict_serde() -> Any:
    """Strict checkpoint serializer (no pickle, allowlist only)."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    return JsonPlusSerializer(pickle_fallback=False,
                              allowed_msgpack_modules=None)


def open_async_strict_saver(db_path: Any) -> Any:
    """Async context manager yielding an AsyncSqliteSaver (strict serde).

    The maintained AG-UI agent is async-only, so devmode runs need the
    async saver (`aiosqlite`, already a pinned dep). Same strictness as
    the sync bridge saver: no pickle fallback.
    """
    import contextlib as _cl

    @_cl.asynccontextmanager
    async def _ctx() -> Any:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        conn = await aiosqlite.connect(str(db_path))
        try:
            yield AsyncSqliteSaver(conn, serde=_strict_serde())
        finally:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
    return _ctx()


async def acollect_agui_events(agent: ToolAwareAgent,
                               run_input: Any) -> list[dict]:
    """Drive one vendored AG-UI run to completion; return wire events."""
    import enum as _enum
    out: list[dict] = []
    async for ev in agent.run(run_input):
        try:
            t = getattr(ev, "type", None)
            # Wire value ("RUN_FINISHED"), not the enum repr.
            tval = t.value if isinstance(t, _enum.Enum) else str(t)
            try:
                data = ev.model_dump(exclude_none=True)
            except Exception:  # noqa: BLE001
                data = {}
            out.append({"type": str(tval), "data": data})
        except Exception:  # noqa: BLE001
            continue
    return out


def collect_agui_events(agent: ToolAwareAgent, run_input: Any) -> list[dict]:
    """Drive one vendored AG-UI run to completion; return wire events."""
    import asyncio

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in agent.run(run_input):
            try:
                t = getattr(ev, "type", None)
                # Pydantic v2 AG-UI events: dump minimal wire shape.
                try:
                    data = ev.model_dump(exclude_none=True)
                except Exception:  # noqa: BLE001
                    data = {}
                out.append({"type": str(t), "data": data})
            except Exception:  # noqa: BLE001
                continue
        return out

    return asyncio.run(_go())


def surface_calls_from_messages(messages: list) -> list[dict]:
    """Return surface-owned tool calls from the latest AIMessage."""
    for m in reversed(list(messages or [])):
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            return [c for c in (m.tool_calls or [])
                    if isinstance(c, dict)
                    and c.get("name") in SURFACE_TOOLS]
    return []


def history_to_agui_messages(history: list[tuple[str, str]],
                             current_request: str) -> list[dict]:
    """Bounded owner-scoped turns → AG-UI messages (untrusted data)."""
    import uuid as _uuid
    out: list[dict] = []
    for role, text in history:
        out.append({"id": f"hist-{_uuid.uuid4().hex[:8]}",
                    "role": "user" if role == "user" else "assistant",
                    "content": text})
    # Dedupe trailing duplicate of the current request (phone post-then-task).
    if out and out[-1].get("role") == "user" \
            and (out[-1].get("content") or "").strip() == current_request.strip():
        out = out[:-1]
    return out
