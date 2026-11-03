"""Scoped file tools: every action requires the `files` capability and the
workspace jail. Read results return to the harness so the model can
reason over real content across turns.

Reserved broker state (`.me_runtime/` checkpoint + pending payload) is
denied to all three tools (post-symlink, incl. listings which hide it).
Writes are never silently truncated: per-file and per-run caps fail
closed with `tool_error`/`workspace_denied`.
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from ..errors import ToolDeniedError
from ..workspace import MAX_WRITE_BYTES, Workspace, WorkspaceDenied

# Per-run aggregate quota: safe under existing task budgets (max_tool_calls
# is 20 in the default bridge request) and home-compatible as artifacts.
MAX_WRITE_TOTAL_BYTES = 20_000_000


def make_files_tools(ws: Workspace, capabilities: dict,
                     record_artifact) -> list[Any]:
    if not isinstance(capabilities, dict):
        capabilities = {}

    total_written = 0

    def _require() -> None:
        if not capabilities.get("files"):
            raise ToolDeniedError("capability_denied", "files not granted")

    @tool("files_read")
    def files_read(path: str) -> str:
        """Read a UTF-8 text file inside the run workspace. Args: path."""
        _require()
        if not isinstance(path, str) or not path:
            raise ToolDeniedError("invalid_request",
                                  "files_read path must be a non-empty string")
        try:
            return ws.read_text(path)
        except WorkspaceDenied as exc:
            raise ToolDeniedError("workspace_denied", str(exc)) from exc

    @tool("files_list")
    def files_list(path: str = ".") -> str:
        """List workspace-relative entries under a directory. Args: path."""
        _require()
        if not isinstance(path, str) or not path:
            raise ToolDeniedError("invalid_request",
                                  "files_list path must be a non-empty string")
        try:
            items = ws.list_dir(path)
        except WorkspaceDenied as exc:
            raise ToolDeniedError("workspace_denied", str(exc)) from exc
        return "\n".join(items) if items else "(empty)"

    @tool("files_write")
    def files_write(path: str, content: str) -> str:
        """Write content to a workspace-relative path. Args: path, content."""
        _require()
        nonlocal total_written
        if not isinstance(path, str) or not path:
            raise ToolDeniedError("invalid_request",
                                  "files_write path must be a non-empty string")
        if not isinstance(content, str):
            raise ToolDeniedError("invalid_request",
                                  "files_write content must be a string")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolDeniedError(
                "tool_error",
                f"file too large: {len(data)} bytes exceeds "
                f"{MAX_WRITE_BYTES} byte per-file cap; write refused, "
                "not truncated")
        if total_written + len(data) > MAX_WRITE_TOTAL_BYTES:
            raise ToolDeniedError(
                "tool_error",
                f"per-run write quota exceeded: {total_written} + "
                f"{len(data)} bytes exceeds {MAX_WRITE_TOTAL_BYTES} "
                "byte aggregate cap; write refused, not truncated")
        try:
            target = ws.write_bytes(path, data)
        except WorkspaceDenied as exc:
            raise ToolDeniedError("workspace_denied", str(exc)) from exc
        total_written += len(data)
        record_artifact(ws.relative(target), data)
        return f"wrote {ws.relative(target)} ({len(content)} chars)"

    return [files_read, files_list, files_write]
