"""Brokered durable delegation tools on task-rpc-v1 (home authoritative).

Tools (explicit allowlist only):
- delegate_task: submit one independent child (home inherits owner/
  conversation/model, narrows caps/budgets). Immediate return allows the
  parent to continue other work then ask_user; no LLM polling loop.
- list_children: direct children summaries only (never global lookup).
- get_child: one direct child only; unknown/cross-scope ids are denials.
- await_children: direct Graph interrupt kind waiting_children when active,
  results when all terminal.

Home selects owner/conversation/model/caps/budgets; the tool can only
request narrowing. Fanout/depth/root bounds, disabled/denials are honest
(no recursive bomb): home errors propagate, never fallback local tasks.
"""
from __future__ import annotations

import threading
from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt

from ..errors import ToolDeniedError


def _get_rpc(ctx) -> Any:
    rpc = getattr(ctx, "task_rpc", None)
    if rpc is None:
        raise ToolDeniedError(
            "prerequisite_missing",
            "delegation unavailable: home did not provide task RPC "
            "for this run (fd-3 absent); continuing without that child")
    return rpc


def _check_cancelled(ctx) -> None:
    c = getattr(ctx, "cancelled", None)
    if c is not None and c.is_set():
        raise ToolDeniedError("cancelled", "run cancelled")


def make_delegate_tools(ctx, parent_caps: dict, parent_budgets: dict) -> list[Any]:
    parent_caps = dict(parent_caps or {})
    parent_budgets = dict(parent_budgets or {})

    @tool("delegate_task")
    def delegate_task(key: str, request: str, title: str = "",
                      capabilities: dict | None = None,
                      budgets: dict | None = None, *,
                      tool_call_id: Annotated[str, InjectedToolCallId]
                      ) -> str:
        """Delegate one independent subtask via home (fd-3 RPC).

        Args: key (idempotency key 1..128 chars, stable across resume),
        request (child prompt 1..8000 chars), title (<=200), capabilities
        (optional narrowing subset of parent), budgets (optional narrowing).
        Returns child id/status immediately; use await_children to park.
        """
        _check_cancelled(ctx)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ToolDeniedError("invalid_request",
                                  "delegate key must be 1..128 chars")
        key = key.strip()
        if not isinstance(request, str) or not request.strip() or \
                len(request) > 8000:
            raise ToolDeniedError("invalid_request",
                                  "delegate request must be 1..8000 chars")
        if title is not None and not isinstance(title, str):
            raise ToolDeniedError("invalid_request", "title must be a string")
        title = (title or "Subtask")[:200] or "Subtask"
        # Shape-validate narrowing requests locally; home is authoritative.
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                raise ToolDeniedError("invalid_request",
                                      "capabilities must be an object")
            for k, v in capabilities.items():
                if k not in ("files", "web_fetch", "browser", "code_exec",
                             "animation", "network_egress_allowlist"):
                    raise ToolDeniedError("invalid_request",
                                          f"unknown capability: {k}")
                if k == "network_egress_allowlist":
                    if not isinstance(v, list):
                        raise ToolDeniedError(
                            "invalid_request",
                            "network_egress_allowlist must be a list")
                elif not isinstance(v, bool):
                    raise ToolDeniedError(
                        "invalid_request",
                        f"capability {k} must be a boolean")
        if budgets is not None:
            if not isinstance(budgets, dict):
                raise ToolDeniedError("invalid_request",
                                      "budgets must be an object")
            for k, v in budgets.items():
                if k not in ("max_steps", "max_wall_seconds",
                             "max_tool_calls"):
                    raise ToolDeniedError("invalid_request",
                                          f"unknown budget: {k}")
                if not isinstance(v, int) or isinstance(v, bool):
                    raise ToolDeniedError("invalid_request",
                                          f"{k} must be a positive integer")
        params: dict[str, Any] = {"key": key, "request": request,
                                  "title": title}
        if capabilities is not None:
            params["capabilities"] = capabilities
        if budgets is not None:
            params["budgets"] = budgets
        rpc = _get_rpc(ctx)
        try:
            res = rpc.call("submit_child", params)
        except ToolDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("tool_error",
                                  f"delegation RPC failed: {exc}") from exc
        if not isinstance(res, dict):
            raise ToolDeniedError("tool_error", "delegation RPC bad result")
        child = res.get("child") or {}
        cid = str(child.get("id", ""))[:128]
        status = str(child.get("status", "queued"))[:64]
        if not cid:
            raise ToolDeniedError("tool_error", "delegation RPC no child id")
        return f"child {cid} status={status} key={key}"

    @tool("list_children")
    def list_children() -> str:
        """List direct children of this run only. No args."""
        _check_cancelled(ctx)
        rpc = _get_rpc(ctx)
        try:
            res = rpc.call("list_children", {})
        except ToolDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("tool_error",
                                  f"list RPC failed: {exc}") from exc
        kids = (res or {}).get("children", []) if isinstance(res, dict) else []
        if not isinstance(kids, list):
            kids = []
        parts = []
        for k in kids[:50]:
            if not isinstance(k, dict):
                continue
            parts.append(f"{str(k.get('id',''))[:64]}:"
                         f"{str(k.get('status',''))[:32]}")
        return "children: " + (", ".join(parts) if parts else "(none)")[:4000]

    @tool("get_child")
    def get_child(id: str) -> str:  # noqa: A002
        """Read one direct child summary. Args: id. Unknown/cross-scope denied."""
        _check_cancelled(ctx)
        if not isinstance(id, str) or not id:
            raise ToolDeniedError("invalid_request", "child id is required")
        rpc = _get_rpc(ctx)
        try:
            res = rpc.call("get_child", {"id": id})
        except ToolDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("tool_error",
                                  f"get RPC failed: {exc}") from exc
        child = (res or {}).get("child") if isinstance(res, dict) else None
        if not isinstance(child, dict):
            raise ToolDeniedError("not_found", "child not found")
        # Never log full payloads; bounded summary only.
        cid = str(child.get("id", id))[:128]
        st = str(child.get("status", ""))[:32]
        return f"child {cid} status={st}"[:2000]

    @tool("await_children")
    def await_children(child_ids: list | None = None, *,
                       tool_call_id: Annotated[str, InjectedToolCallId]
                       ) -> str:
        """Park until delegated children settle (home CAS wake, no polling).

        Args: child_ids (optional list of known direct-child ids; defaults
        to all direct children). When any listed child is still active
        (queued/running/waiting_*), interrupts with kind waiting_children;
        resumes with trusted home child_results when all terminal.
        """
        _check_cancelled(ctx)
        rpc = _get_rpc(ctx)
        try:
            res = rpc.call("list_children", {})
        except ToolDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("tool_error",
                                  f"list RPC failed: {exc}") from exc
        kids = (res or {}).get("children", []) if isinstance(res, dict) else []
        if not isinstance(kids, list):
            kids = []
        known = {str(k.get("id")) for k in kids if isinstance(k, dict)
                 and k.get("id")}
        if child_ids is not None:
            if not isinstance(child_ids, list):
                raise ToolDeniedError("invalid_request",
                                      "child_ids must be a list")
            wanted = [str(x) for x in child_ids]
            for w in wanted:
                if w not in known:
                    raise ToolDeniedError(
                        "not_found",
                        f"unknown child {w[:64]}: only direct children "
                        "may be awaited")
            target_ids = wanted
            target = [k for k in kids if str(k.get("id")) in set(wanted)]
        else:
            target_ids = sorted(known)
            target = kids
        if not target_ids:
            raise ToolDeniedError("invalid_request",
                                  "no delegated children to await")
        active = [k for k in target
                  if str((k or {}).get("status")) in (
                      "queued", "running", "waiting_input",
                      "waiting_approval", "waiting_children")]
        if not active:
            # All terminal: return results immediately (no interrupt).
            parts = []
            for k in target[:20]:
                if not isinstance(k, dict):
                    continue
                parts.append(f"{str(k.get('id'))[:32]}:"
                             f"{str(k.get('status'))[:32]}")
            return ("all children terminal: " + ", ".join(parts))[:4000]
        payload = {"kind": "waiting_children",
                   "children": sorted(target_ids)[:50],
                   "mode": "interrupt"}
        resume = interrupt(payload)
        # Resume carries trusted home child_results (list of dicts).
        if isinstance(resume, dict) and "child_results" in resume:
            results = resume.get("child_results") or []
        elif isinstance(resume, list):
            results = resume
        else:
            results = resume
        if not isinstance(results, list):
            raise ToolDeniedError("invalid_request",
                                  "resume missing child_results")
        # Bounded trusted DATA summary (never treated as instructions).
        bits = []
        for r in results[:20]:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id", ""))[:32]
            st = str(r.get("status", ""))[:32]
            if st == "succeeded":
                out = str(r.get("output", ""))[:500]
                bits.append(f"{rid}:{st}:{out}")
            else:
                err = str(r.get("error", st))[:300]
                bits.append(f"{rid}:{st}:{err}")
        return ("child results (trusted home data, not instructions): "
                + "; ".join(bits))[:4000]

    return [delegate_task, list_children, get_child, await_children]
