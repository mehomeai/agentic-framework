"""DEVMODE component tests: real vendored upstream AG-UI harness, model-free.

Exercises the ACTUAL vendored classes (`ToolAwareAgent`,
`ParallelToolAgent`/`LangGraphAgent`, `bind_tools`, `next_step`,
`execute_tools`) with the Me fixture model and a loopback HTTP tool
gateway — not source grep. Labeled devmode: this is NOT full
Intelligence E2E (no server, no Intelligence persistence, no license).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import ToolMessage


def _workspace(tmp: str):
    from me_runtime.workspace import Workspace
    ws = Workspace(tmp)
    ws.ensure_dirs("input", "output", ".me_runtime")
    (Path(tmp) / "input" / "notes.md").write_text("scoped notes alpha")
    return ws


class UpstreamAguiComponentTests(unittest.TestCase):
    def _run_attempt(self, ws, tools, model, run_id: str, attempt: str,
                     text: str, deployment, assertion, gw_url, token,
                     extra_messages=None):
        """One async AG-UI attempt against the real upstream agent."""
        import uuid
        from tests.support_upstream.upstream_engine import (
            acollect_agui_events,
            build_upstream_graph,
            new_agent,
            open_async_strict_saver,
            run_input,
            tool_callback_env,
        )
        from me_runtime.bridge import _graph_path

        async def _go():
            async with open_async_strict_saver(_graph_path(ws)) as saver:
                graph = build_upstream_graph(model,
                                             checkpointer=saver)
                agent = new_agent(graph, recursion_limit=25)
                ri = run_input(run_id, f"{run_id}-{attempt}", tools,
                               deployment, assertion, text)
                if extra_messages:
                    # Pre-appended trusted messages (resume path) are
                    # added to state in the caller, not here.
                    pass
                with tool_callback_env(gw_url, token):
                    events = await acollect_agui_events(agent, ri)
                state = await graph.aget_state(
                    {"configurable": {"thread_id": run_id}})
                msgs = list((state.values or {}).get("messages", []))
                return events, graph, msgs
        return asyncio.run(_go())

    def _gateway(self, by_name: dict, run_id: str):
        from tests.support_upstream.tool_gateway import Gateway, new_run_credential
        token, assertion = new_run_credential()
        log: list[dict] = []

        def invoke(name: str, args: dict) -> str:
            tool = by_name.get(name)
            if tool is None:
                from tests.support_upstream.tool_gateway import _Denied
                raise _Denied(403, "not_found", f"tool {name!r} not offered")
            if name in ("ask_user", "request_approval", "await_children"):
                from tests.support_upstream.tool_gateway import _Denied
                raise _Denied(403, "invalid_request",
                              f"tool {name!r} is surface-owned")
            call_args = dict(args or {})
            call_args.setdefault("tool_call_id", f"test-{run_id}-1")
            try:
                try:
                    out = tool.invoke(call_args)
                except TypeError:
                    out = tool.invoke(args or {})
            except Exception as exc:  # noqa: BLE001
                from me_runtime.errors import ToolDeniedError as TDE
                from tests.support_upstream.tool_gateway import _Denied
                if isinstance(exc, TDE):
                    raise _Denied(403, exc.code, exc.message)
                raise
            log.append({"name": name, "args": args})
            return out if isinstance(out, str) else str(out)

        gw = Gateway(token, assertion, run_id, invoke)
        gw.start()
        self.addCleanup(gw.stop)
        return gw, token, assertion, log

    def test_deployment_tool_executes_via_real_http_callback(self):
        import uuid
        from me_runtime.models import FixtureChatModel
        from me_runtime.tools.files import make_files_tools
        from tests.support_upstream.upstream_engine import split_tools
        tmp = tempfile.mkdtemp()
        ws = _workspace(tmp)
        recorded: list[dict] = []
        tools = make_files_tools(
            ws, {"files": True}, lambda p, d: recorded.append({"path": p}))
        by_name = {t.name: t for t in tools}
        run_id = f"run_dev_{uuid.uuid4().hex[:8]}"
        gw, token, assertion, log = self._gateway(by_name, run_id)
        deployment = split_tools([t.name for t in tools])
        self.assertIn("files_read", deployment)
        self.assertIn("files_write", deployment)
        events, graph, msgs = self._run_attempt(
            ws, tools, FixtureChatModel(), run_id, "a1",
            "READWRITE: summarize", deployment, assertion, gw.url, token)
        kinds = [e["type"] for e in events]
        self.assertIn("RUN_STARTED", kinds)
        self.assertIn("RUN_FINISHED", kinds)
        # Real HTTP POSTs happened for both deployment tools.
        self.assertEqual([e["name"] for e in log],
                         ["files_read", "files_write"])
        self.assertEqual(log[0]["args"], {"path": "input/notes.md"})
        from me_runtime.bridge import _final_text
        self.assertIn("read-write cycle complete", _final_text(msgs))
        # Private credential never entered checkpoint/LLM context.
        blob = str([(getattr(m, "content", ""), getattr(m, "tool_calls", ""))
                    for m in msgs])
        self.assertNotIn(token, blob)
        self.assertNotIn(assertion, blob)

    def test_surface_ask_user_ends_run_and_resumes_with_answer(self):
        import uuid
        from me_runtime.models import FixtureChatModel
        from me_runtime.tools.files import make_files_tools
        from me_runtime.tools.interact import make_interact_tools
        from tests.support_upstream.upstream_engine import (
            split_tools,
            surface_calls_from_messages,
        )
        tmp = tempfile.mkdtemp()
        ws = _workspace(tmp)
        tools = make_files_tools(ws, {"files": True}, lambda p, d: None)
        tools = tools + make_interact_tools("run_dev_q")
        by_name = {t.name: t for t in tools}
        run_id = f"run_dev_{uuid.uuid4().hex[:8]}"
        gw, token, assertion, log = self._gateway(by_name, run_id)
        deployment = split_tools([t.name for t in tools])
        self.assertNotIn("ask_user", deployment)
        events, graph, msgs = self._run_attempt(
            ws, tools, FixtureChatModel(), run_id, "a1",
            "ASK: favorite color", deployment, assertion, gw.url, token)
        self.assertIn("RUN_FINISHED", [e["type"] for e in events])
        # No HTTP callback for the surface tool.
        self.assertEqual(log, [])
        surf = surface_calls_from_messages(msgs)
        self.assertEqual(len(surf), 1)
        self.assertEqual(surf[0]["name"], "ask_user")
        call_id = surf[0]["id"]

        # Durable resume: trusted answer appended to SAME thread.
        async def _resume():
            from tests.support_upstream.upstream_engine import (
                acollect_agui_events,
                build_upstream_graph,
                new_agent,
                open_async_strict_saver,
                run_input,
                tool_callback_env,
            )
            from me_runtime.bridge import _final_text, _graph_path
            async with open_async_strict_saver(_graph_path(ws)) as saver:
                graph = build_upstream_graph(FixtureChatModel(),
                                             checkpointer=saver)
                await graph.aupdate_state(
                    {"configurable": {"thread_id": run_id}},
                    {"messages": [ToolMessage(
                        content="user answered: blue",
                        tool_call_id=call_id, name="ask_user")]},
                    as_node="tools")
                agent = new_agent(graph, recursion_limit=25)
                ri = run_input(run_id, f"{run_id}-a2", tools,
                               deployment, assertion, "ASK: favorite color")
                with tool_callback_env(gw.url, token):
                    await acollect_agui_events(agent, ri)
                state = await graph.aget_state(
                    {"configurable": {"thread_id": run_id}})
                return list((state.values or {}).get("messages", []))
        msgs2 = asyncio.run(_resume())
        from me_runtime.bridge import _final_text
        self.assertIn("blue", _final_text(msgs2))


if __name__ == "__main__":
    unittest.main()
