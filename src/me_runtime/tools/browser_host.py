"""Host controller for the isolated interactive browser.

Production path is Docker ONLY: one `docker run -i` container per browser
session with `--network none`, unprivileged uid, cap-drop, no-new-privs,
read-only root, tmpfs caps, and a read-only worker-code mount. The worker
has no NIC; every page request is fulfilled here via the vetted broker
(`broker_fetch_raw`: allowlist + pinned DNS/TLS, per-hop validation,
credential-strip on cross-origin redirect, bounded sizes, no proxy/curlrc).
GET/HEAD are allowed by default; any other method requires `allow_write=True`,
which the host sets only inside an approved-action window.

There is deliberately NO environment/API/LLM switch that enables a host
browser in production: without Docker + the provisioned image the tool
fails closed with `prerequisite_missing`. Synthetic-DOM tests inject a
worker through an explicit `popen_factory` (see `test_worker_popen_factory`,
test-only) — never through an env var read by the production spawner.
"""
from __future__ import annotations

import base64
import errno
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import ToolDeniedError
from ..workspace import Workspace
from . import browser_protocol as proto
from .web import broker_fetch_raw

BROWSER_IMAGE_ENV = "ME_RUNTIME_DOCKER_IMAGE_BROWSER"
DEFAULT_BROWSER_IMAGE = "me-browser-playwright:v1.64.1"
WORKER_FILENAME = "browser_worker.py"
CMD_TIMEOUT_S = 60.0

_owned_lock = threading.Lock()
_owned: dict[str, set[str]] = {}
_managers_lock = threading.Lock()
_managers: dict[str, list] = {}


def register_manager(run_id: str, mgr) -> None:
    with _managers_lock:
        _managers.setdefault(run_id, []).append(mgr)


def close_managers_for_run(run_id: str) -> None:
    with _managers_lock:
        mgrs = _managers.pop(run_id, [])
    for m in mgrs:
        try:
            m.close_all()
        except Exception:  # noqa: BLE001
            pass


def sanitize_name(run_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", run_id)[:32]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def image_present(image: str) -> bool:
    try:
        from ..task_rpc import sanitize_child_env as _sanitize
        env = _sanitize()
    except Exception:  # noqa: BLE001
        env = None
    try:
        r = subprocess.run(["docker", "image", "inspect", image],
                           capture_output=True, timeout=30,
                           env=env, close_fds=True)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def browser_container_name(run_id: str, session_tag: str) -> str:
    return f"me-br-{sanitize_name(run_id)}-{re.sub(r'[^a-zA-Z0-9_.-]', '-', session_tag)[:16]}"


def _register(run_id: str, cname: str) -> None:
    with _owned_lock:
        _owned.setdefault(run_id, set()).add(cname)


def _unregister(run_id: str, cname: str) -> None:
    with _owned_lock:
        s = _owned.get(run_id)
        if s is not None:
            s.discard(cname)


def kill_run_browser_containers(run_id: str) -> None:
    """Best-effort cancel cleanup: kill + remove owned browser containers."""
    if not docker_available():
        return
    try:
        from ..task_rpc import sanitize_child_env as _sanitize
        env = _sanitize()
    except Exception:  # noqa: BLE001
        env = None
    with _owned_lock:
        names = sorted(_owned.get(run_id, set()))
    for name in names:
        try:
            subprocess.run(["docker", "kill", name], capture_output=True,
                           timeout=15, env=env, close_fds=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                           timeout=15, env=env, close_fds=True)
        except Exception:  # noqa: BLE001
            pass


def build_browser_docker_cmd(image: str, cname: str,
                             worker_dir_ro: str) -> list[str]:
    """Locked-down container invocation (no NIC, no writable host mounts)."""
    return ["docker", "run", "-i", "--rm", "--pull", "never",
            "--name", cname,
            "--network", "none",
            "--user", "65532:65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", "1.0", "--memory", "512m",
            "--pids-limit", "128",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "-e", "HOME=/tmp",
            "--workdir", "/tmp",
            "-v", f"{worker_dir_ro}:/worker:ro",
            image, "python", "/worker/browser_worker.py"]


def worker_dir() -> str:
    return str(Path(__file__).resolve().parent)


class JsonlFramer:
    """Bounded binary JSONL framer (one JSON object per `\\n` line).

    Binary-safe: partial frames stay buffered, batched frames are all
    returned in order, oversize frames/lines fail closed. `stdout` can
    never grow unbounded — the buffer is capped at MAX_FRAME_BYTES + 1KiB
    slack for a partial line.
    """

    def __init__(self, cap: int = proto.MAX_FRAME_BYTES) -> None:
        self.cap = cap
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        if data:
            self.buf.extend(data)
            if len(self.buf) > self.cap + 1024:
                raise ValueError("worker frame exceeds bounded cap")
        out: list[dict] = []
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > self.cap + 1024:
                    raise ValueError("worker frame exceeds bounded cap")
                break
            raw = bytes(self.buf[:nl])
            del self.buf[:nl + 1]
            if not raw.strip():
                continue
            if len(raw) > self.cap:
                raise ValueError("worker frame exceeds bounded cap")
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"bad worker frame: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError("frame must be an object")
            out.append(obj)
        return out


def _read_bounded(path: Path, cap: int) -> bytes | None:
    """Bounded file read: never buffers more than cap+1 bytes."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
    except OSError:
        return None
    if data is None:
        return None
    if len(data) > cap:
        return None  # oversize: treat as absent/invalid, never buffer more
    return data


def test_worker_popen_factory(
        executable: str | None = None):
    """Test-only DI spawner for synthetic-DOM smoke (never production).

    Returns a `popen_factory(session_id, cname)` that runs the worker
    directly on the host. Production `BrowserManager._spawn` never calls
    this; tests pass it explicitly. No env var enables it.
    """
    def _factory(session_id: str, cname: str):
        from ..task_rpc import sanitize_child_env as _sanitize
        env = _sanitize()
        if executable:
            env["ME_BROWSER_TEST_EXECUTABLE"] = executable
        return subprocess.Popen(
            [sys.executable, str(Path(worker_dir()) / WORKER_FILENAME)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, env=env,
            close_fds=True)
    return _factory


class _Proc:
    """One owned worker process (Docker container or test-DI worker)."""

    def __init__(self, popen: subprocess.Popen, cname: str,
                 run_id: str, test_only: bool) -> None:
        self.popen = popen
        self.cname = cname
        self.run_id = run_id
        # True only for explicitly injected test workers; production Docker
        # containers always use test_only=False (docker rm on close).
        self.test_only = test_only

    def close(self) -> None:
        try:
            for stream in (getattr(self.popen, "stdin", None),
                           getattr(self.popen, "stdout", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                self.popen.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.popen.kill()
                except Exception:  # noqa: BLE001
                    pass
        if not self.test_only:
            # Docker was invoked with `--rm`, but remove explicitly anyway
            # so cancelled/killed containers never linger.
            try:
                from ..task_rpc import sanitize_child_env as _san2
                _env = _san2()
            except Exception:  # noqa: BLE001
                _env = None
            try:
                subprocess.run(["docker", "rm", "-f", self.cname],
                               capture_output=True, timeout=30,
                               env=_env, close_fds=True)
            except Exception:  # noqa: BLE001
                pass
        _unregister(self.run_id, self.cname)


class BrowserManager:
    """Per-bridge-process session manager (scoped to run/user/conversation)."""

    def __init__(self, ws: Workspace, capabilities: dict, run_id: str,
                 user_id: str, conversation_id: str,
                 record_artifact, cancelled,
                 popen_factory=None) -> None:
        self.ws = ws
        self.allowlist = capabilities.get("network_egress_allowlist", []) or []
        self.run_id = run_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.record_artifact = record_artifact
        self.cancelled = cancelled
        self._popen_factory = popen_factory
        self._sessions: dict[str, _Proc] = {}
        self._seq = 0
        self._cmd_seq = 0
        self._lock = threading.Lock()
        register_manager(run_id, self)

    # -- session bookkeeping -------------------------------------------
    def _session_dir(self, session_id: str) -> Path:
        # Session ids are generated as br_<run>_<n>; reject path tricks.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session_id):
            raise ToolDeniedError("invalid_request", "bad session id")
        return self.ws.resolve(f".me_runtime/browser/{session_id}")

    def _check_binding(self, session_id: str) -> None:
        if not session_id.startswith("br_"):
            raise ToolDeniedError("invalid_request", "unknown browser session")
        raw = _read_bounded(self._session_dir(session_id) / "meta.json",
                            8192)
        if raw is None:
            raise ToolDeniedError("invalid_request",
                                  "unknown browser session") from None
        try:
            meta = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise ToolDeniedError("invalid_request",
                                  "unknown browser session") from None
        for f in ("run_id", "user_id", "conversation_id"):
            if meta.get(f) != getattr(self, f):
                raise ToolDeniedError(
                    "invalid_request",
                    "browser session does not belong to this run/user/"
                    "conversation")

    def _save_session(self, session_id: str, url: str,
                      storage_b64: str) -> None:
        d = self._session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "meta.json").write_text(json.dumps({
                "run_id": self.run_id, "user_id": self.user_id,
                "conversation_id": self.conversation_id,
                "session_id": session_id, "url": (url or "")[:2048]}))
            if storage_b64:
                raw = base64.b64decode(storage_b64.encode("ascii"),
                                       validate=True)
                if len(raw) <= proto.MAX_STORAGE_STATE_BYTES:
                    (d / "storage.json").write_bytes(raw)
        except Exception:  # noqa: BLE001
            pass

    def _load_storage(self, session_id: str) -> str:
        raw = _read_bounded(
            self._session_dir(session_id) / "storage.json",
            proto.MAX_STORAGE_STATE_BYTES)
        if not raw:
            return ""
        return base64.b64encode(raw).decode("ascii")

    def _alloc_path(self) -> Path:
        return self.ws.resolve(".me_runtime/browser/_alloc.json")

    def _load_alloc(self) -> dict:
        raw = _read_bounded(self._alloc_path(), 65536)
        if raw is None:
            return {"seq": 0, "by_tool_call": {}}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"seq": 0, "by_tool_call": {}}
        if not isinstance(data, dict):
            return {"seq": 0, "by_tool_call": {}}
        seq = data.get("seq", 0)
        mapping = data.get("by_tool_call", {})
        if not isinstance(seq, int) or not isinstance(mapping, dict):
            return {"seq": 0, "by_tool_call": {}}
        return {"seq": max(seq, 0),
                "by_tool_call": {str(k)[:128]: str(v)[:80]
                                 for k, v in mapping.items() if k and v}}

    def _save_alloc(self, data: dict) -> None:
        try:
            parent = self._alloc_path().parent
            parent.mkdir(parents=True, exist_ok=True)
            tmp = parent / "_alloc.tmp"
            tmp.write_text(json.dumps(
                {"seq": int(data.get("seq", 0)),
                 "by_tool_call": dict(data.get("by_tool_call", {}))}))
            tmp.rename(self._alloc_path())
        except Exception:  # noqa: BLE001
            pass

    def new_session_id(self, tool_call_id: str | None = None) -> str:
        """Allocate a collision-free session id, persisted across resumes.

        Keyed by the LangGraph tool-call id when available: replaying the
        same tool call (interrupt resume, harness retry) returns the
        identical session id, so the approval `args`/`args_hash` stays
        stable; parallel calls with distinct ids get distinct sessions.
        """
        key = (tool_call_id or "").strip()[:128]
        with self._lock:
            alloc = self._load_alloc()
            if key:
                mapped = alloc["by_tool_call"].get(key)
                if mapped and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", mapped):
                    return mapped
            seq = int(alloc.get("seq", 0))
            base = f"br_{sanitize_name(self.run_id)}"
            for _ in range(1000):
                seq += 1
                sid = f"{base}_{seq}"
                try:
                    if not (self._session_dir(sid) / "meta.json").exists():
                        break
                except Exception:  # noqa: BLE001
                    break
            alloc["seq"] = seq
            if key:
                alloc["by_tool_call"][key] = sid
                # Bound the map: keep the most recent entries only.
                while len(alloc["by_tool_call"]) > 64:
                    alloc["by_tool_call"].pop(
                        next(iter(alloc["by_tool_call"])))
            self._save_alloc(alloc)
            return sid

    # -- process spawning (production: Docker ONLY, no env switch) ------
    def _spawn(self, session_id: str) -> _Proc:
        if self.cancelled is not None and self.cancelled.is_set():
            raise ToolDeniedError("cancelled", "run cancelled")
        tag = session_id[-16:]
        cname = browser_container_name(self.run_id, tag)
        if self._popen_factory is not None:
            # Test-only dependency injection (synthetic worker). No env
            # var, API field, or model request can select this path in
            # production: only an explicitly constructed manager carries
            # a factory, and bridge/agent construction never sets one.
            popen = self._popen_factory(session_id, cname)
            proc = _Proc(popen, cname, self.run_id, True)
            _register(self.run_id, cname)
            return proc
        if not docker_available():
            raise ToolDeniedError(
                "prerequisite_missing",
                "isolated browser needs Docker + provisioned browser image "
                "(provisioned by setup-infra); host execution is never a "
                "fallback")
        image = os.environ.get(BROWSER_IMAGE_ENV, DEFAULT_BROWSER_IMAGE)
        if not image_present(image):
            raise ToolDeniedError(
                "prerequisite_missing",
                f"browser image {image!r} not present; refusing to pull "
                "or run on the host")
        cmd = build_browser_docker_cmd(image, cname, worker_dir())
        try:
            from ..task_rpc import sanitize_child_env as _san3
            _env = _san3()
        except Exception:  # noqa: BLE001
            _env = None
        try:
            popen = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     bufsize=0, env=_env, close_fds=True)
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("tool_error",
                                  "browser container start failed") from exc
        proc = _Proc(popen, cname, self.run_id, False)
        _register(self.run_id, cname)
        return proc

    def _proc_for(self, session_id: str) -> _Proc:
        proc = self._sessions.get(session_id)
        if proc is not None and proc.popen.poll() is None:
            return proc
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
            self._sessions.pop(session_id, None)
        proc = self._spawn(session_id)
        self._sessions[session_id] = proc
        return proc

    # -- protocol (bounded binary framer, deadline + cancel) ------------
    def _write_frame(self, proc: _Proc, obj: dict) -> None:
        assert proc.popen.stdin is not None
        try:
            line = proto.encode_frame(obj)
        except ValueError as exc:
            raise ToolDeniedError("invalid_request", str(exc)) from exc
        try:
            proc.popen.stdin.write(line.encode("utf-8") + b"\n")
            proc.popen.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ToolDeniedError("tool_error",
                                  "browser worker pipe broken") from exc

    def _exchange(self, proc: _Proc, op: str, payload: dict,
                  allow_write: bool) -> dict:
        if self.cancelled is not None and self.cancelled.is_set():
            raise ToolDeniedError("cancelled", "run cancelled")
        assert proc.popen.stdin is not None and proc.popen.stdout is not None
        with self._lock:
            self._cmd_seq += 1
            cmd_id = self._cmd_seq
        cmd = {"v": 1, "id": cmd_id, "op": op, **payload}
        self._write_frame(proc, cmd)
        framer = JsonlFramer()
        out_fd = proc.popen.stdout.fileno()
        deadline = time.time() + CMD_TIMEOUT_S
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._kill_proc(proc)
                raise ToolDeniedError("tool_error",
                                      "browser command timed out; "
                                      "container killed")
            if self.cancelled is not None and self.cancelled.is_set():
                self._kill_proc(proc)
                raise ToolDeniedError("cancelled", "run cancelled")
            rlist, _, _ = select.select([out_fd], [], [],
                                        min(remaining, 0.2))
            if not rlist:
                if proc.popen.poll() is not None:
                    # Drain any final buffered frames before reporting exit.
                    try:
                        chunk = os.read(out_fd, 65536)
                    except OSError:
                        chunk = b""
                    if chunk:
                        try:
                            msgs = framer.feed(chunk)
                        except ValueError as exc:
                            raise ToolDeniedError(
                                "tool_error",
                                f"bad worker frame: {exc}") from exc
                        res = self._handle_frames(
                            proc, cmd_id, msgs, allow_write)
                        if res is not None:
                            return res
                    raise ToolDeniedError("tool_error",
                                          "browser worker exited")
                continue
            try:
                chunk = os.read(out_fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise ToolDeniedError("tool_error",
                                      "browser worker pipe broken") from exc
            if not chunk:
                raise ToolDeniedError("tool_error",
                                      "browser worker exited")
            try:
                msgs = framer.feed(chunk)
            except ValueError as exc:
                self._kill_proc(proc)
                raise ToolDeniedError("tool_error",
                                      f"bad worker frame: {exc}") from exc
            res = self._handle_frames(proc, cmd_id, msgs, allow_write)
            if res is not None:
                return res

    def _handle_frames(self, proc: _Proc, cmd_id: int, msgs: list[dict],
                       allow_write: bool) -> dict | None:
        for msg in msgs:
            if msg.get("id") != cmd_id:
                continue
            mtype = msg.get("type")
            if mtype == "fetch":
                self._serve_fetch(proc, cmd_id, msg, allow_write)
                continue
            if mtype == "result":
                if msg.get("ok"):
                    payload_out = msg.get("payload", {})
                    if not isinstance(payload_out, dict):
                        raise ToolDeniedError("tool_error",
                                              "bad worker result")
                    return payload_out
                raise ToolDeniedError("tool_error",
                                      str(msg.get("error", "worker failed"))
                                      [:500])
        return None

    def _serve_fetch(self, proc: _Proc, cmd_id: int, msg: dict,
                     allow_write: bool) -> None:
        fetch_id = str(msg.get("fetch_id", ""))
        method = str(msg.get("method", "GET")).upper()
        url = str(msg.get("url", ""))[:2048]
        headers = msg.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        body_b64 = str(msg.get("body_b64", "") or "")
        body: bytes | None = None
        if body_b64:
            try:
                body = proto.b64d(body_b64, proto.MAX_FRAME_BYTES)
            except ValueError:
                body = None
            if body is not None and len(body) > 256_000:
                body = None
        try:
            # Background page subrequests must not smuggle writes: only
            # GET/HEAD flow unless this command runs inside the approved
            # window for the exact gated action.
            if method not in ("GET", "HEAD") and not allow_write:
                self._write_frame(proc, {"v": 1, "id": cmd_id,
                                         "type": "fetch_error",
                                         "fetch_id": fetch_id,
                                         "error": "write blocked without "
                                                  "approved action"})
                return
            status, _final, rheaders, rbody = broker_fetch_raw(
                method, url, self.allowlist, dict(list(headers.items())[:32]),
                body, allow_write)
            self._write_frame(proc, {"v": 1, "id": cmd_id,
                                     "type": "fetch_response",
                                     "fetch_id": fetch_id, "status": status,
                                     "headers": dict(
                                         list(rheaders.items())[:32]),
                                     "body_b64": base64.b64encode(
                                         rbody).decode("ascii")})
        except ToolDeniedError as exc:
            try:
                self._write_frame(proc, {"v": 1, "id": cmd_id,
                                         "type": "fetch_error",
                                         "fetch_id": fetch_id,
                                         "error": f"{exc.code}"})
            except ToolDeniedError:
                pass
        except (BrokenPipeError, OSError):
            pass

    def _kill_proc(self, proc: _Proc) -> None:
        try:
            if not proc.test_only:
                try:
                    from ..task_rpc import sanitize_child_env as _san4
                    _env = _san4()
                except Exception:  # noqa: BLE001
                    _env = None
                subprocess.run(["docker", "kill", proc.cname],
                               capture_output=True, timeout=15,
                               env=_env, close_fds=True)
            else:
                proc.popen.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.close()
        except Exception:  # noqa: BLE001
            pass

    # -- public ops ------------------------------------------------------
    def open(self, url: str, session_id: str = "",
             tool_call_id: str | None = None) -> tuple[str, dict]:
        proto.validate_url(url)
        sid = session_id or self.new_session_id(tool_call_id)
        if session_id:
            self._check_binding(sid)
        else:
            self._save_session(sid, url, "")
        proc = self._proc_for(sid)
        storage = self._load_storage(sid)
        snap = self._exchange(proc, "open",
                              {"url": url, "storage_state_b64": storage},
                              False)
        self._save_session(sid, str(snap.get("url", url)),
                           str(snap.get("storage_state_b64", "") or storage))
        self._maybe_artifact(sid, snap)
        return sid, snap

    def snapshot(self, session_id: str) -> dict:
        self._check_binding(session_id)
        proc = self._proc_for(session_id)
        snap = self._exchange(proc, "snapshot", {}, False)
        self._save_session(session_id, str(snap.get("url", "")),
                           str(snap.get("storage_state_b64", "")
                               or self._load_storage(session_id)))
        return snap

    def act(self, session_id: str, actions: list[dict],
            allow_write: bool = False) -> dict:
        self._check_binding(session_id)
        clean = proto.validate_actions(actions)
        proc = self._proc_for(session_id)
        snap = self._exchange(proc, "act", {"actions": clean}, allow_write)
        self._save_session(session_id, str(snap.get("url", "")),
                           str(snap.get("storage_state_b64", "")
                               or self._load_storage(session_id)))
        self._maybe_artifact(session_id, snap)
        return snap

    def screenshot(self, session_id: str) -> dict:
        self._check_binding(session_id)
        proc = self._proc_for(session_id)
        snap = self._exchange(proc, "screenshot", {}, False)
        self._maybe_artifact(session_id, snap)
        return snap

    def _maybe_artifact(self, session_id: str, snap: dict) -> None:
        shot_b64 = str(snap.get("screenshot_b64", "") or "")
        if not shot_b64:
            return
        try:
            raw = base64.b64decode(shot_b64.encode("ascii"), validate=True)
        except Exception:  # noqa: BLE001
            return
        if not raw.startswith(b"\x89PNG") or len(raw) > 1_000_000:
            return
        short = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)[:24]
        try:
            target = self.ws.write_bytes(
                f"output/browser/screenshot-{short}.png", raw)
            self.record_artifact(self.ws.relative(target), raw)
            snap["screenshot_artifact"] = self.ws.relative(target)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # Never retain bulky base64 in checkpointed messages.
            snap.pop("screenshot_b64", None)

    def close_all(self) -> None:
        for sid, proc in list(self._sessions.items()):
            try:
                try:
                    self._exchange(proc, "close", {}, False)
                except ToolDeniedError:
                    pass
            finally:
                try:
                    proc.close()
                except Exception:  # noqa: BLE001
                    pass
                self._sessions.pop(sid, None)

    def drop_session(self, session_id: str) -> None:
        proc = self._sessions.pop(session_id, None)
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
