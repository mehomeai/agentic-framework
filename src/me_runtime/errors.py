"""Shared error type for policy denials raised by brokered tools.

Tools raise ToolDeniedError("<code>: <message>"). The LangGraph ToolNode
converts these into error ToolMessages for the model; the fixture stub and
the bridge map the leading code onto terminal `failed` results.
"""
from __future__ import annotations


class ToolDeniedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
