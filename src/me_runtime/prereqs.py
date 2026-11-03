"""Prerequisite report: optional heavy backends (honest, never faked)."""
from __future__ import annotations

import os
import shutil


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def report() -> dict:
    docker = bool(shutil.which("docker"))
    return {
        "harness": "langgraph-prebuilt-react",
        "playwright": _importable("playwright"),
        "chromium": bool(shutil.which("chromium") or shutil.which("chrome")
                         or shutil.which("google-chrome")
                         or shutil.which("chromium-browser")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "pillow": _importable("PIL"),
        "docker": docker,
        "sandbox": os.environ.get("ME_RUNTIME_SANDBOX", ""),
        "node": bool(shutil.which("node")),
        "ollama_endpoint": os.environ.get("ME_RUNTIME_LOCAL_ENDPOINT",
                                         "http://127.0.0.1:11434"),
    }
