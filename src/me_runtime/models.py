"""Chat-model adapters on maintained SDK interfaces.

- `local`     → langchain-ollama ChatOllama (local-first, no key).
- `openai`    → langchain-openai ChatOpenAI against an explicitly configured
  remote OpenAI-compatible endpoint, only with ME_RUNTIME_ALLOW_REMOTE=1.
  Supports `use_responses_api: true` where the installed SDK accepts it.
- `anthropic` → langchain-anthropic ChatAnthropic, same opt-in rule.
- `fixture`   → deterministic offline FixtureChatModel implementing the
  LangChain chat-model interface (tool-call round trips through the real
  harness). TEST-ONLY: instantiated only for provider=fixture; its
  ASK:/APPROVE:/etc. directives are never a production planner.

Secrets come only from environment variables named by `api_key_env`; the
request itself must never carry a key, and keys are never logged or echoed
into events.
"""
from __future__ import annotations

import itertools
import os
import sys
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

RUN_FAILED_PREFIX = "RUN-FAILED"


class ModelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ModelConfig:
    provider: str
    model: str
    endpoint: str | None = None
    api_key_env: str | None = None
    use_responses_api: bool = False


def model_config_from(obj: dict[str, Any]) -> ModelConfig:
    if not isinstance(obj, dict):
        raise ModelError("invalid_request", "model must be an object")
    provider = obj.get("provider")
    model = obj.get("model")
    if not provider or not isinstance(provider, str):
        raise ModelError("invalid_request", "model.provider is required")
    if not model or not isinstance(model, str):
        raise ModelError("invalid_request", "model.model is required")
    return ModelConfig(
        provider=provider,
        model=model,
        endpoint=obj.get("endpoint"),
        api_key_env=obj.get("api_key_env"),
        use_responses_api=bool(obj.get("use_responses_api", False)),
    )


def _require_remote_opt_in(provider: str, api_key_env: str | None) -> str:
    if os.environ.get("ME_RUNTIME_ALLOW_REMOTE") != "1":
        raise ModelError(
            "model_not_configured",
            f"remote provider {provider!r} requires "
            "ME_RUNTIME_ALLOW_REMOTE=1 (explicit opt-in)")
    env_name = api_key_env or "ME_RUNTIME_MODEL_API_KEY"
    key = os.environ.get(env_name)
    if not key:
        raise ModelError("model_not_configured",
                         f"remote provider {provider!r} needs {env_name} set")
    return key


def build_chat_model(cfg: ModelConfig):
    """Return a LangChain chat model for the configured provider."""
    provider = cfg.provider
    if provider == "fixture":
        return FixtureChatModel()
    if provider == "local":
        from langchain_ollama import ChatOllama
        base_url = (cfg.endpoint or os.environ.get(
            "ME_RUNTIME_LOCAL_ENDPOINT", "http://127.0.0.1:11434"))
        # Privacy: Ollama python client forwards kwargs to httpx (trust_env
        # defaults True, so HTTP_PROXY could route loopback prompts off-host).
        # Pin loopback transport to ignore ambient proxy env. Remote providers
        # stay explicit opt-in and unchanged.
        return ChatOllama(model=cfg.model, base_url=base_url,
                          temperature=0.2,
                          sync_client_kwargs={"trust_env": False},
                          async_client_kwargs={"trust_env": False})
    if provider == "openai":
        # Any provider=openai use — even a localhost URL — requires the
        # explicit remote opt-in so a remote URL can never be mislabelled
        # to skip privacy checks.
        key = _require_remote_opt_in("openai", cfg.api_key_env)
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "model": cfg.model, "api_key": key, "temperature": 0.2}
        if cfg.endpoint:
            kwargs["base_url"] = cfg.endpoint
        if cfg.use_responses_api:
            try:
                kwargs["use_responses_api"] = True
                return ChatOpenAI(**kwargs)
            except TypeError:
                del kwargs["use_responses_api"]
                print("[runtime] installed langchain-openai does not "
                      "accept use_responses_api; using Chat Completions",
                      file=sys.stderr)
        return ChatOpenAI(**kwargs)
    if provider == "anthropic":
        key = _require_remote_opt_in("anthropic", cfg.api_key_env)
        from langchain_anthropic import ChatAnthropic
        kwargs = {"model": cfg.model, "api_key": key, "temperature": 0.2}
        if cfg.endpoint:
            kwargs["base_url"] = cfg.endpoint
        return ChatAnthropic(**kwargs)
    raise ModelError("invalid_request",
                     f"unknown model.provider {provider!r}")


# ---------------------------------------------------------------------------
# Deterministic offline test double (TEST-ONLY).
# ---------------------------------------------------------------------------

_KNOWN_CODES = ("capability_denied", "workspace_denied", "budget_exhausted",
                "prerequisite_missing", "approval_rejected", "tool_error",
                "model_error", "model_not_configured", "invalid_request",
                "internal", "cancelled")


def _tool_error_code(content: str) -> str:
    head = (content or "")[:300]
    for code in _KNOWN_CODES:
        if code in head:
            return code
    return "tool_error"


class FixtureChatModel(BaseChatModel):
    """Deterministic stub driving real tool-call round trips (tests only).

    Interprets test directives in the first human message and answers with
    standard `tool_calls`, consuming real tool observations over multiple
    turns. Never used outside provider=fixture.
    """

    _counter: Any = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._counter = itertools.count(1)

    @property
    def _llm_type(self) -> str:
        return "me-fixture"

    def bind_tools(self, tools, **kwargs: Any):
        """Harness calls this to obtain a tool-capable model.

        The stub already emits standard `tool_calls` for the brokered
        tools, so it returns itself; unknown kwargs (tool_choice, …)
        are accepted and ignored.
        """
        return self

    def _call_id(self) -> str:
        # Unique across bridge processes/resumes: LangGraph 1.x matches
        # ToolMessage to AIMessage via tool_call_id, so per-process counters
        # resetting to 1 would duplicate checkpointed ids and misroute.
        import uuid as _uuid
        return f"fixture_call_{next(self._counter)}_{_uuid.uuid4().hex[:8]}"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Current request is the last NON-continuation HumanMessage:
        # bounded history turns precede it; the completed_early continuation
        # appends a "[home child results:" HumanMessage last, which is data,
        # never directives. Directives parse from current only.
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        non_cont = [m for m in humans
                    if not (isinstance(m.content, str)
                            and "[home child results:" in m.content)]
        human = non_cont[-1] if non_cont else (humans[-1] if humans else None)
        text = human.content if human and isinstance(human.content, str) \
            else ""
        history_texts: list[str] = []
        for m in messages:
            if m is human:
                continue
            if isinstance(m, HumanMessage) and isinstance(m.content, str):
                if m.content.strip():
                    history_texts.append(m.content[:2000])
            elif isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                c = m.content
                if isinstance(c, str) and c.strip():
                    history_texts.append(c[:2000])
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") in (
                                "text", "output_text"):
                            t = b.get("text")
                            if isinstance(t, str) and t.strip():
                                history_texts.append(t[:2000])
                            break
        calls = [m for m in messages if isinstance(m, AIMessage)
                 and getattr(m, "tool_calls", None)]
        used = [tc["name"] for m in calls for tc in (m.tool_calls or [])]
        observations: dict[str, str] = {}
        error_obs: dict[str, str] = {}
        for m in messages:
            if m.type == "tool":
                name = getattr(m, "name", "") or ""
                content = m.content if isinstance(m.content, str) else ""
                observations[name] = content
                if getattr(m, "status", None) == "error":
                    error_obs[name] = content

        def _call(name: str, args: dict) -> ChatResult:
            ai = AIMessage(content="",
                           tool_calls=[{"name": name, "args": args,
                                        "id": self._call_id(),
                                        "type": "tool_call"}])
            return ChatResult(generations=[ChatGeneration(message=ai)])

        def _finish(content: str) -> ChatResult:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content=content))])

        def _fail(code: str, msg: str) -> ChatResult:
            return _finish(f"{RUN_FAILED_PREFIX} {code}: {msg}"[:2000])

        def _after(name: str) -> ChatResult | None:
            if name in error_obs:
                return _fail(_tool_error_code(error_obs[name]),
                             error_obs[name][:500])
            return None

        directive_lines = [ln.strip() for ln in text.splitlines()
                           if ln.strip() and not ln.startswith("[home context")]
        first = directive_lines[0] if directive_lines else ""

        if first.startswith("FAIL:"):
            return _fail("tool_error",
                         first[5:].strip() or "fixture failure")

        # TEST-ONLY empty final: returns no new text so the bridge must
        # not substitute a prior historical assistant answer. Honest
        # empty collapses to "done" upstream, never a stale secret.
        if first.startswith("EMPTY_FINAL"):
            ai = AIMessage(content="")
            return ChatResult(generations=[ChatGeneration(message=ai)])

        # TEST-ONLY context recall: proves bounded history turns reached
        # the maintained harness. Returns prior user/assistant texts plus
        # the current request (no tools, no planner).
        if "CONTEXT_ECHO" in text:
            hist = " | ".join(history_texts[:10])[:3000]
            return _finish(
                f"context echo history=[{hist}] current=[{text[:500]}]"[:3500])

        # Child sleep (cancellable long run for cascade tests).
        if first.startswith("SLEEP:"):
            try:
                secs = float(first[len("SLEEP:"):].strip() or "0")
            except ValueError:
                secs = 0
            # Deterministic: fixture completes quickly unless the run is
            # cancelled (bridge kills the process). Cap to avoid hangs.
            if secs > 0 and "sleep_done" not in observations:
                # Signal via tool? No: just finish after bounded wait is
                # handled by the home wall/cancel. For cascade tests the
                # parent parks while this child runs; the child itself
                # sleeps briefly then completes (cancellable by SIGTERM).
                import time as _t
                _t.sleep(min(secs, 5))
                return _finish("fixture-delegation-echo: slept")
            return _finish("fixture-delegation-echo: slept")

        # --- Durable delegation directives (TEST-ONLY) ---
        # DELEGATE:<key>:<request> lines spawn one child each via fd-3 RPC.
        # DELEGATE_WAIT parks via await_children; ASK: parks via ask_user.
        # Child runs reuse existing directives (e.g. READWRITE) so the real
        # home+runtime path is exercised, not a fake planner.
        delegates: list[tuple[str, str]] = []
        for ln in directive_lines:
            if ln.startswith("DELEGATE:"):
                rest = ln[len("DELEGATE:"):]
                sep = rest.find(":")
                if sep > 0:
                    k = rest[:sep].strip()
                    r = rest[sep + 1:].strip()
                    if k and r:
                        delegates.append((k, r))
        dup_key = dup_req = None
        for ln in directive_lines:
            if ln.startswith("DELEGATE_DUP:"):
                rest = ln[len("DELEGATE_DUP:"):]
                sep = rest.find(":")
                if sep > 0:
                    dup_key = rest[:sep].strip()
                    dup_req = rest[sep + 1:].strip()
                break
        wide_req = None
        for ln in directive_lines:
            if ln.startswith("DELEGATE_WIDE:"):
                rest = ln[len("DELEGATE_WIDE:"):]
                sep = rest.find(":")
                if sep > 0:
                    wide_req = (rest[:sep].strip(), rest[sep + 1:].strip())
                else:
                    wide_req = (rest.strip() or "widekey", "wide child")
                break
        want_wait = "DELEGATE_WAIT" in text
        want_early = "COMPLETE_EARLY" in text
        # Continuation after completed_early park: bridge injected trusted
        # home data as a later HumanMessage. Aggregate once, never re-park.
        continuation_data = ""
        for m in messages:
            if isinstance(m, HumanMessage) and isinstance(m.content, str) \
                    and "[home child results:" in m.content:
                continuation_data = m.content
                break
        # Count delegate_task calls already checkpointed (no replay).
        delegate_calls = [tc for m in calls for tc in (m.tool_calls or [])
                          if tc.get("name") == "delegate_task"]
        # Widening attempt: must be denied honestly, no fallback.
        if wide_req is not None:
            if "delegate_task" not in used:
                wk, wr = wide_req
                return _call("delegate_task",
                             {"key": wk or "widekey",
                              "request": wr or "wide child",
                              "capabilities": {"files": True,
                                               "code_exec": True}})
            done = _after("delegate_task")
            if done:
                return done
            # If widening was NOT denied, the tool succeeded — honest failure.
            return _fail("internal", "widening was not denied")
        # Idempotency probe: same key twice must yield same child; different
        # payload must conflict. Home is authoritative; we surface honestly.
        if dup_key is not None and dup_req:
            if len(delegate_calls) == 0:
                return _call("delegate_task",
                             {"key": dup_key, "request": dup_req})
            if len(delegate_calls) == 1:
                if "delegate_task" in error_obs:
                    return _fail(_tool_error_code(
                        error_obs["delegate_task"]),
                        error_obs["delegate_task"][:500])
                return _call("delegate_task",
                             {"key": dup_key, "request": dup_req})
            if len(delegate_calls) == 2:
                if "delegate_task" in error_obs:
                    return _fail(_tool_error_code(
                        error_obs["delegate_task"]),
                        error_obs["delegate_task"][:500])
                # Different payload, same key must conflict.
                return _call("delegate_task",
                             {"key": dup_key,
                              "request": dup_req + " different-payload"})
            if len(delegate_calls) == 3:
                # Third call must have conflicted; otherwise honest failure.
                if "delegate_task" not in error_obs:
                    return _fail("internal",
                                 "different payload did not conflict")
                if "delegation_conflict" not in error_obs["delegate_task"]:
                    return _fail(_tool_error_code(
                        error_obs["delegate_task"]),
                        error_obs["delegate_task"][:500])
                # Conflict seen as expected; clear error path by awaiting.
                if want_wait and "await_children" not in used:
                    return _call("await_children", {})
                done = _after("await_children")
                if done:
                    return done
                return _finish(
                    f"dup ok children={observations.get('await_children','')}"[:1000])
        if delegates:
            # Continuation after premature-completed park: aggregate injected
            # trusted data once, never re-early-complete (no endless loop).
            if continuation_data:
                return _finish(
                    f"parent done children={continuation_data[:800]}"[:1000])
            if len(delegate_calls) < len(delegates):
                # Surface denials honestly, no recursive bomb.
                if "delegate_task" in error_obs:
                    return _fail(_tool_error_code(
                        error_obs["delegate_task"]),
                        error_obs["delegate_task"][:500])
                k, r = delegates[len(delegate_calls)]
                return _call("delegate_task", {"key": k, "request": r})
            # All spawns done; surface spawn denials honestly.
            if "delegate_task" in error_obs:
                return _fail(_tool_error_code(
                    error_obs["delegate_task"]),
                    error_obs["delegate_task"][:500])
            if want_early:
                # Premature completed with active children: bridge must hold
                # as waiting_children (completion policy), not publish.
                return _finish("fixture-delegation-echo: early completed "
                               "with active children")
            if want_wait:
                if "await_children" not in used:
                    return _call("await_children", {})
                done = _after("await_children")
                if done:
                    return done
                # Aggregate trusted home data (success + failure honestly).
                return _finish(
                    f"parent done children={observations.get('await_children','')[:800]}"[:1000])
            if "ASK:" in text:
                prompt = text.split("ASK:", 1)[1].splitlines()[0].strip()
                if "ask_user" not in used:
                    return _call("ask_user",
                                 {"id": "q1",
                                  "prompt": prompt or "more input needed"})
                done = _after("ask_user")
                if done:
                    return done
                # After answer, collect completed but unseen child outputs
                # before final aggregation (no endless reinjection).
                if "list_children" not in used:
                    return _call("list_children", {})
                done = _after("list_children")
                if done:
                    return done
                if "await_children" not in used:
                    return _call("await_children", {})
                done = _after("await_children")
                if done:
                    return done
                return _finish(
                    f"answer received: {observations.get('ask_user','')[:300]} "
                    f"children={observations.get('await_children','')[:500]}"[:1000])
        if first.startswith("WRITE_THEN_ASK:"):
            prompt = first.split("WRITE_THEN_ASK:", 1)[1].strip() or \
                "more input needed"
            if "files_write" not in used:
                return _call("files_write",
                             {"path": "output/legacy_effect.md",
                              "content": f"legacy-effect: {prompt[:300]}\n"})
            done = _after("files_write")
            if done:
                return done
            if "ask_user" not in used:
                return _call("ask_user",
                             {"id": "q1",
                              "prompt": prompt[:500] or "more input needed"})
            done = _after("ask_user")
            if done:
                return done
            return _finish(f"answer received: "
                           f"{observations.get('ask_user', '')}"[:1000])

        # Await with no children must fail honestly (no silent success).
        if want_wait and not delegates and dup_key is None and \
                wide_req is None:
            if "await_children" not in used:
                return _call("await_children", {})
            done = _after("await_children")
            if done:
                return done
            return _finish(
                f"parent done children={observations.get('await_children','')[:800]}"[:1000])

        if "ASK:" in text:
            prompt = text.split("ASK:", 1)[1].splitlines()[0].strip()
            if "ask_user" not in used:
                return _call("ask_user",
                             {"id": "q1",
                              "prompt": prompt or "more input needed"})
            done = _after("ask_user")
            if done:
                return done
            return _finish(f"answer received: "
                           f"{observations.get('ask_user', '')}"[:1000])

        if "APPROVE:" in text:
            desc = text.split("APPROVE:", 1)[1].splitlines()[0].strip()
            if "request_approval" not in used:
                return _call("request_approval",
                             {"id": "a1", "action": desc or "gated-action",
                              "params": {"note": desc or "gated-action"},
                              "reason": "fixture approval gate"})
            done = _after("request_approval")
            if done:
                return done
            if "files_write" not in used:
                return _call("files_write",
                             {"path": "output/approval_record.md",
                              "content": f"approved action: {desc}\n"
                                         f"decision: "
                                         f"{observations.get('request_approval', '')}\n"})
            done = _after("files_write")
            if done:
                return done
            return _finish(f"approved action done: {desc}"[:500])

        if "READWRITE:" in text:
            if "files_read" not in used:
                return _call("files_read", {"path": "input/notes.md"})
            done = _after("files_read")
            if done:
                return done
            if "files_write" not in used:
                observed = observations.get("files_read", "")
                return _call("files_write",
                             {"path": "output/summary.md",
                              "content": f"summary of input: {observed[:300]}"})
            done = _after("files_write")
            if done:
                return done
            return _finish("read-write cycle complete")

        if "WRITE_CHILD:" in text:
            body = text.split("WRITE_CHILD:", 1)[1].strip().splitlines()[0]
            if "files_write" not in used:
                return _call("files_write",
                             {"path": "output/child.md",
                              "content": f"child wrote: {body[:300]}"})
            done = _after("files_write")
            if done:
                return done
            return _finish(f"child done: {body[:200]}")

        if "RUN_CODE:" in text:
            code = text.split("RUN_CODE:", 1)[1].strip().splitlines()[0]
            if "run_code" not in used:
                return _call("run_code", {"language": "python",
                                          "code": code or "print('hi')"})
            done = _after("run_code")
            if done:
                return done
            return _finish(f"code result: "
                           f"{observations.get('run_code', '')}"[:1000])

        if "FETCH:" in text:
            url = text.split("FETCH:", 1)[1].strip().splitlines()[0]
            if "web_fetch" not in used:
                return _call("web_fetch", {"url": url})
            done = _after("web_fetch")
            if done:
                return done
            return _finish(f"fetched: "
                           f"{observations.get('web_fetch', '')}"[:1000])

        if "ANIMATE:" in text:
            title = text.split("ANIMATE:", 1)[1].strip().splitlines()[0]
            if "animation_build" not in used:
                return _call("animation_build",
                             {"title": title or "untitled", "caption": ""})
            done = _after("animation_build")
            if done:
                return done
            return _finish(f"animation ready: "
                           f"{observations.get('animation_build', '')}"[:500])

        if "BROWSER_ACT:" in text:
            url = text.split("BROWSER_ACT:", 1)[1].strip().splitlines()[0]
            if "browser_open" not in used:
                return _call("browser_open",
                             {"url": url,
                              "actions": [{"op": "click", "target": "e0"}]})
            done = _after("browser_open")
            if done:
                return done
            return _finish(f"acted: "
                           f"{observations.get('browser_open', '')}"[:1000])

        if "BROWSER:" in text:
            url = text.split("BROWSER:", 1)[1].strip().splitlines()[0]
            if "browser_open" not in used:
                return _call("browser_open", {"url": url})
            done = _after("browser_open")
            if done:
                return done
            return _finish(f"browsed: "
                           f"{observations.get('browser_open', '')}"[:1000])

        # Structured-content finals (TEST-ONLY): emit the final answer as a
        # real AIMessage content-block list through the harness, mirroring
        # modern provider styles. Exercises bridge extraction end to end.
        if first.startswith("STRUCTURED_RESP:"):
            body = first.split("STRUCTURED_RESP:", 1)[1].strip() \
                .splitlines()[0] or "structured"
            ai = AIMessage(content=[{"type": "output_text",
                                     "text": body[:1000]}])
            return ChatResult(generations=[ChatGeneration(message=ai)])
        if first.startswith("STRUCTURED_MIXED:"):
            body = first.split("STRUCTURED_MIXED:", 1)[1].strip() \
                .splitlines()[0] or "structured"
            ai = AIMessage(content=[
                {"type": "reasoning", "reasoning": "secret-must-not-leak"},
                {"type": "text", "text": body[:1000]}])
            return ChatResult(generations=[ChatGeneration(message=ai)])
        if first.startswith("STRUCTURED:"):
            body = first.split("STRUCTURED:", 1)[1].strip() \
                .splitlines()[0] or "structured"
            ai = AIMessage(content=[{"type": "text",
                                     "text": body[:1000]}])
            return ChatResult(generations=[ChatGeneration(message=ai)])

        return _finish("fixture completed request")
