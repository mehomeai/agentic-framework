"""Image-recipe regression tests: the lock is consumed, hashes anchored.

Covers independent-review findings on the production build recipe:
constraints-me.txt is COPY'd + `pip install -c` (not a floating unused
file), the generated Dockerfile CMD is an all-string exec array, every
staged file is verified against the in-repo trusted MANIFEST (not
self-comparison), vendor provenance is checked BEFORE copying, and the
stage script refuses symlink/foreign destinations. No Docker, no network.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from me_runtime import agent_service as svc


def _vendor() -> Path:
    return (Path(__file__).resolve().parent.parent
            / "src" / "me_runtime" / "upstream" / "vendor")


def test_pinned_recipe_is_upstream_base_and_patch_is_build_only():
    manifest = svc._manifest()
    assert svc._sha256_bytes(
        svc.pinned_dockerfile().encode()) == manifest["dockerfile"]["base_sha256"]
    assert svc._sha256_bytes(
        svc.patched_dockerfile().encode()) == manifest["dockerfile"]["patched_sha256"]
    # BUILD-001 only: base + constraints COPY/apply, nothing else.
    old = ("COPY agent-langgraph-agui/requirements.txt ./\n"
           "RUN pip install --no-cache-dir -r requirements.txt\n")
    new = ("COPY agent-langgraph-agui/requirements.txt ./\n"
           "COPY constraints-me.txt ./\n"
           "RUN pip install --no-cache-dir -r requirements.txt "
           "-c constraints-me.txt\n")
    assert svc.patched_dockerfile() == svc.pinned_dockerfile().replace(old, new)
    assert [p["id"] for p in manifest["dockerfile"]["patches"]] == ["BUILD-001"]


def test_generated_cmd_is_all_string_exec_form():
    dockerfile = svc.patched_dockerfile()
    cmds = [ln for ln in dockerfile.splitlines()
            if ln.strip().startswith("CMD ")]
    assert len(cmds) == 1
    argv = json.loads(cmds[0].split(None, 1)[1])
    assert isinstance(argv, list) and argv
    assert all(isinstance(a, str) and a for a in argv), argv
    assert argv[-2:] == ["--port", "4206"]


def test_constraints_consumed_by_recipe_and_hash_anchored():
    dockerfile = svc.patched_dockerfile()
    assert "COPY constraints-me.txt ./" in dockerfile
    assert "-c constraints-me.txt" in dockerfile
    manifest = svc._manifest()
    assert svc._sha256_bytes(
        ("\n".join(svc.RESOLVED_PINS) + "\n").encode()) == \
        manifest["generated"]["constraints_txt_sha256"]
    assert svc._sha256_bytes(
        svc.UPSTREAM_REQUIREMENTS.encode()) == \
        manifest["generated"]["requirements_txt_sha256"]


def test_vendor_verified_before_build_and_tamper_fails(tmp_path):
    assert svc.verify_vendor_files() == []
    good = tmp_path / "vendor"
    shutil.copytree(_vendor(), good, ignore=shutil.ignore_patterns("__pycache__"))
    assert svc.verify_vendor_files(good) == []
    (good / "main.py").write_bytes(
        (good / "main.py").read_bytes() + b"# tampered\n")
    problems = svc.verify_vendor_files(good)
    assert problems, "tampered vendor must fail provenance"
    try:
        svc.build_context(tmp_path / "ctx", vendor_dir=good)
    except ValueError as exc:
        assert "vendor provenance failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("build_context staged a tampered vendor")


def test_staged_requirements_or_sources_drift_fails_verify(tmp_path):
    dest = tmp_path / "ctx"
    svc.build_context(dest)
    assert svc.verify_build_context(dest) == []
    (dest / "agent-langgraph-agui" / "requirements.txt").write_text(
        "langgraph\n")
    assert svc.verify_build_context(dest) != []
    svc.build_context(dest)  # re-run over owned context restores
    (dest / "agent-langgraph-agui" / "src" / "main.py").write_bytes(b"tampered")
    assert any("main.py" in p for p in svc.verify_build_context(dest))


def test_tool_callback_url_never_double_appends():
    full = svc.tool_callback_url("http://127.0.0.1:3001")
    assert full == "http://127.0.0.1:3001/api/agent-tools/call"
    # Passing the already-full endpoint must not double the suffix.
    assert svc.tool_callback_url(full + "/") == full
    manifest = svc.service_manifest()
    assert manifest["tool_callback"].startswith("POST {OPENBOT_TOOL_URL}")


def test_ollama_container_route_is_container_reachable():
    host = svc.ollama_route()
    assert host["OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    in_image = svc.ollama_route(in_container=True)
    assert in_image["OPENAI_BASE_URL"] == \
        "http://host.docker.internal:11434/v1"
    assert in_image["BOT_PROVIDER"] == "openai"
    explicit = svc.ollama_route("http://custom:11434", in_container=True)
    assert explicit["OPENAI_BASE_URL"] == "http://custom:11434/v1"


def test_stage_script_refuses_symlink_and_foreign_dir(tmp_path):
    script = (Path(__file__).resolve().parent.parent
              / "scripts" / "stage_agent_image_context.py")
    real = tmp_path / "ctx"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    proc = subprocess.run([sys.executable, str(script), str(link)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "symlink" in proc.stderr
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "Dockerfile").write_text("FROM scratch\n")
    proc = subprocess.run([sys.executable, str(script), str(foreign)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "foreign" in proc.stderr
    # Owned re-run is safe: stage, then stage again over it.
    owned = tmp_path / "owned"
    first = subprocess.run([sys.executable, str(script), str(owned)],
                           capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, str(script), str(owned)],
                            capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr
    assert svc.verify_build_context(owned) == []
