"""Upstream's own vendored suite runs green against our staged tree.

`src/me_runtime/upstream/tests/` holds verbatim copies of the pinned
`agent-langgraph-agui/tests/` suite (hash-verified). This module stages
the runnable upstream layout (see `test_upstream_entrypoint`) and runs
that suite as-is: the strongest cheap proof that our vendor copy is
unmodified upstream and that a future pin bump surfaces regressions
here first. No live spend (upstream fixtures are loopback/socket-guarded).

Gating is conditional on the environment (never a broad skip):
cases run wherever their dependency exists. On this Mac (no
`langchain-google-genai`, no `docker`) 11 cases deselect with reasons,
proved honest by `test_upstream_vendored_suite_gates_are_environmental`;
Linux CI installs the package and has docker, so the full suite runs.
  - 9 google-provider cases: need the `langchain-google-genai` package
    (image-build dep, installed in CI);
  - 2 chatgpt atomic-writer cases: shell out to `docker` (present in CI).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from tests.test_upstream_entrypoint import (
    _sha256,
    _vendor_dir,
    stage_upstream_tree,
)

STAGED_TESTS = ("conftest.py", "test_model_spec.py",
                "test_parallel_tool_events.py",
                "test_provider_boundaries.py", "test_spec_validation.py",
                "test_tool_protocol.py")

# (node-ID substring, dependency probe): gated only while the dep is absent.
GATED_SUBSTRINGS = (
    ("test_google_provider_reaches_google_genai_boundary",
     "langchain_google_genai"),
    ("test_google_spec_model_reaches_google_request",
     "langchain_google_genai"),
    ("test_chatgpt_token_provider_atomic_writer_survives_directory_mount",
     "docker"),
    ("test_chatgpt_token_provider_atomic_writer_fails_on_single_file_mount",
     "docker"),
)


def _dep_present(probe: str) -> bool:
    if probe == "docker":
        return shutil.which("docker") is not None
    return importlib.util.find_spec(probe) is not None


def _unavailable() -> tuple[str, ...]:
    return tuple(name for name, probe in GATED_SUBSTRINGS
                 if not _dep_present(probe))



def _staged(tmp_path_factory) -> Path:
    from me_runtime.agent_service import verify_vendor_files
    assert verify_vendor_files() == []
    root = tmp_path_factory.mktemp("openbot-suite")
    stage_upstream_tree(root)
    vendor = _vendor_dir()
    agent = root / "agent-langgraph-agui"
    for name in ("main.py", "tool_runtime.py", "parallel_tools.py",
                 "chatgpt_store.py"):
        assert _sha256(agent / "src" / name) == _sha256(vendor / name), name
    for name in ("model_providers.py", "model-providers.json"):
        assert _sha256(root / "shared" / name) == _sha256(vendor / name), \
            name
    return agent / "tests"


def _run_suite(tests_dir: Path, extra: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tests_dir), "-q",
         "-p", "no:cacheprovider", *extra],
        capture_output=True, text=True, timeout=240, cwd=str(tests_dir))


def test_upstream_vendored_suite_passes(tmp_path_factory):
    tests_dir = _staged(tmp_path_factory)
    missing = _unavailable()
    extra = ["-k", " and ".join(f"not {g}" for g in missing)] if missing else []
    proc = _run_suite(tests_dir, extra)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "failed" not in proc.stdout.splitlines()[-1]


def test_upstream_vendored_suite_gates_are_environmental(tmp_path_factory):
    """Deselected cases fail ONLY for stated environmental reasons.

    Vacuous (pass) where every dependency is installed — e.g. Linux CI
    runs the whole suite with no gates.
    """
    missing = _unavailable()
    if not missing:
        return
    names = [g for g, probe in GATED_SUBSTRINGS if g in missing]
    tests_dir = _staged(tmp_path_factory)
    proc = _run_suite(tests_dir, ["-k", " or ".join(names)])
    assert proc.returncode != 0  # they really are gated here
    blob = proc.stdout + proc.stderr
    if any("google" in g for g in names):
        assert "langchain-google-genai" in blob  # missing package
    if any("atomic_writer" in g for g in names):
        assert "'docker'" in blob  # no docker binary
    # No other failure signature: every FAILED line is a gated case.
    failed = [ln for ln in blob.splitlines() if ln.startswith("FAILED")]
    assert failed, blob[-2000:]
    assert all(any(g in ln for g in names) for ln in failed), failed
