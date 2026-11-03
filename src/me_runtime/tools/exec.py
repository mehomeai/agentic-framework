"""Bounded generated-code execution: Docker container or nothing.

There is intentionally NO host fallback. `run_code` executes only inside
a one-shot Docker container with:
- NO writable host mounts: the entry script and workspace input are
  mounted read-only (`/src`, `/input`); ALL container writes go to a
  64MB tmpfs `/work` (plus a 64MB noexec tmpfs `/tmp`), so generated
  code cannot fill home disk regardless of container memory limits;
- container files are EPHEMERAL: tmpfs contents vanish when the
  container stops, so generated files are never collected back. The
  bounded stdout/stderr streams ARE the durable artifacts (saved under
  `output/exec/`). Code that must produce files should print them
  (or encode them) to stdout;
- `--network none`, unprivileged uid 65532, `--cap-drop ALL`,
  `no-new-privileges`, CPU/memory/pid limits, `--workdir /work`,
- no host directories, sockets, or credentials; images must already be
  provisioned (never pulled here).

Stdout/stderr stream with hard byte caps WHILE the process runs
(reader threads, never unbounded `capture_output`). Timeout or
cancellation kills and removes the owned container. No `docker cp`
is attempted: tmpfs contents are destroyed when the container stops,
so post-stop collection cannot recover them — the streams are the
artifacts.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from langchain_core.tools import tool

from ..errors import ToolDeniedError
from ..task_rpc import sanitize_child_env
from ..workspace import Workspace

IMAGES = {
    "python": ("ME_RUNTIME_DOCKER_IMAGE_PY", "python:3.12-slim"),
    "node": ("ME_RUNTIME_DOCKER_IMAGE_NODE", "node:24-slim"),
}
COMMANDS = {
    "python": ["python", "/src/main.py"],
    "node": ["node", "/src/main.js"],
}
ENTRYPOINTS = {"python": "main.py", "node": "main.js"}

RUN_TIMEOUT_S = int(os.environ.get("ME_RUNTIME_CODE_TIMEOUT_S", "60"))
STDOUT_CAP = 1_000_000
STDERR_CAP = 256_000



def sanitize_name(run_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", run_id)[:48]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def container_name(run_id: str) -> str:
    return f"me-rt-{sanitize_name(run_id)}"


def kill_run_containers(run_id: str) -> None:
    """Best-effort cancellation cleanup: kill then remove owned containers."""
    if not docker_available():
        return
    env = sanitize_child_env()
    name = container_name(run_id)
    subprocess.run(["docker", "kill", name], capture_output=True, timeout=15,
                   env=env, close_fds=True)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                   timeout=15, env=env, close_fds=True)


def build_docker_cmd(image: str, cname: str, src_ro: str, input_ro: str,
                     command: list[str]) -> list[str]:
    # No --rm: the container is removed explicitly in a finally block so
    # `docker cp` can extract bounded outputs after stop. No writable host
    # mounts exist at all: /src and /input are read-only, /work and /tmp
    # are size-bounded tmpfs.
    return ["docker", "run", "--name", cname,
            "--network", "none",
            "--user", "65532:65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", "1.0", "--memory", "256m",
            "--pids-limit", "64",
            "--workdir", "/work",
            "--read-only",
            "--tmpfs", "/work:rw,size=64m",
            "--tmpfs", "/tmp:rw,size=64m,noexec",
            "-v", f"{src_ro}:/src:ro",
            "-v", f"{input_ro}:/input:ro",
            image, *command]


def docker_rm(container: str) -> None:
    subprocess.run(["docker", "rm", "-f", container], capture_output=True,
                   timeout=30, env=sanitize_child_env(), close_fds=True)


def _popen_bounded(cmd: list[str], timeout_s: float,
                   cancelled) -> tuple[int, str, str]:
    """Run cmd, streaming stdout/stderr with caps while it runs.

    Returns (returncode, stdout_tail, stderr_tail). Raises
    subprocess.TimeoutExpired on timeout; checks `cancelled` while polling.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=sanitize_child_env(), close_fds=True)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_len = 0
    err_len = 0

    def _drain(stream, chunks: list[bytes], cap: int, counter: str):
        nonlocal out_len, err_len
        while True:
            data = stream.read(65536)
            if not data:
                break
            chunks.append(data)
            if counter == "out":
                out_len += len(data)
            else:
                err_len += len(data)
            # Drop oldest chunks past the cap (bounded memory).
            total = sum(len(c) for c in chunks)
            while total > cap and len(chunks) > 1:
                total -= len(chunks.pop(0))

    tout = threading.Thread(target=_drain,
                            args=(proc.stdout, out_chunks, STDOUT_CAP, "out"),
                            daemon=True)
    terr = threading.Thread(target=_drain,
                            args=(proc.stderr, err_chunks, STDERR_CAP, "err"),
                            daemon=True)
    tout.start()
    terr.start()
    deadline = time.time() + timeout_s
    while True:
        try:
            rc = proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancelled.is_set() or time.time() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                tout.join(timeout=5)
                terr.join(timeout=5)
                raise
    tout.join(timeout=10)
    terr.join(timeout=10)
    stdout = b"".join(out_chunks)[-STDOUT_CAP:].decode("utf-8",
                                                       errors="replace")
    stderr = b"".join(err_chunks)[-STDERR_CAP:].decode("utf-8",
                                                       errors="replace")
    return rc, stdout, stderr


def image_present(image: str) -> bool:
    inspect = subprocess.run(["docker", "image", "inspect", image],
                             capture_output=True, timeout=30,
                             env=sanitize_child_env(), close_fds=True)
    return inspect.returncode == 0


def _prepare_scratch(ws: Workspace) -> Any:
    ws.ensure_dirs(".me_runtime/sandbox", "input")
    scratch = ws.resolve(".me_runtime/sandbox")
    os.chmod(scratch, 0o755)  # host-written entry only; container mounts ro
    return scratch


def make_exec_tools(ws: Workspace, capabilities: dict, run_id: str,
                    record_artifact, cancelled) -> list[Any]:
    @tool("run_code")
    def run_code(language: str, code: str) -> str:
        """Execute code in a one-shot Docker sandbox. Args: language, code."""
        if not capabilities.get("code_exec"):
            raise ToolDeniedError("capability_denied", "code_exec not granted")
        if os.environ.get("ME_RUNTIME_SANDBOX") != "docker":
            raise ToolDeniedError(
                "capability_denied",
                "code_exec requires ME_RUNTIME_SANDBOX=docker on the home "
                "host; host execution is never a fallback")
        if not docker_available():
            raise ToolDeniedError(
                "prerequisite_missing",
                "docker CLI not available (provisioned by setup-infra); "
                "generated code was not executed")
        language = (language or "").lower()
        if language not in IMAGES:
            raise ToolDeniedError("invalid_request",
                                  f"unsupported language {language!r}")
        env_name, default_image = IMAGES[language]
        image = os.environ.get(env_name, default_image)
        # Never pull implicitly: the image must already be provisioned.
        if not image_present(image) or cancelled.is_set():
            if cancelled.is_set():
                raise ToolDeniedError("cancelled", "run cancelled")
            raise ToolDeniedError(
                "prerequisite_missing",
                f"container image {image!r} not present; refusing to pull "
                "or run on the host")
        scratch = _prepare_scratch(ws)
        entry = scratch / ENTRYPOINTS[language]
        entry.write_text(code[:100_000], encoding="utf-8")
        input_ro = str(ws.resolve("input"))
        cname = container_name(run_id)
        cmd = build_docker_cmd(image, cname, str(scratch), input_ro,
                               COMMANDS[language])
        try:
            try:
                rc, stdout, stderr = _popen_bounded(cmd, RUN_TIMEOUT_S,
                                                    cancelled)
            except subprocess.TimeoutExpired:
                kill_run_containers(run_id)
                raise ToolDeniedError("tool_error",
                                      f"code_exec timed out after "
                                      f"{RUN_TIMEOUT_S}s; container killed "
                                      "and removed") from None
            if cancelled.is_set():
                kill_run_containers(run_id)
                raise ToolDeniedError("cancelled", "run cancelled")
            if rc != 0:
                raise ToolDeniedError(
                    "tool_error",
                    f"container exit {rc}: {(stderr or stdout)[-2000:]}")
        finally:
            docker_rm(cname)
        # The bounded streams are the durable artifacts: container files
        # lived on tmpfs and are gone with the container (ephemeral by
        # design). Streams are already capped by _popen_bounded.
        for name, content in (("stdout.txt", stdout or "(no output)"),
                              ("stderr.txt", stderr or "")):
            data = content.encode("utf-8")[-STDOUT_CAP:]
            target = ws.write_bytes(f"output/exec/{name}", data)
            record_artifact(ws.relative(target), data)
        return (stdout or "(no output)")[-20000:]

    return [run_code]
