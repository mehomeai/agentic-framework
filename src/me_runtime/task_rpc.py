"""Thread-safe fd-3 duplex JSONL client for task-rpc-v1 (home authoritative).

Transport (frozen contract `runtime-task-rpc-v1`):
- Node home spawns the bridge with stdio ['pipe','pipe','pipe','pipe'] and
  env ME_HOME_TASK_RPC_FD=3. Main stdin stays exactly one JSON object read
  to EOF (runtime-bridge-v1); delegation uses the extra duplex pipe.
- fd 3 is a duplex JSONL stream owned by the child (possession is the
  capability). Generated Docker code never inherits it.
- Bounds (home-enforced, runtime also bounds its writer):
  64KiB max RPC line, 512KiB max buffered without newline, 200 requests per
  run, 32KiB max params object, 128KiB max response, 256KiB max pending
  socket writes. IDs 1..128 chars or safe ints; methods 1..64 chars; params
  must be objects. 201st request gets one delegation_denied error and pipe
  closes. Split UTF-8 across chunks decoded safely; partial lines buffer
  until newline and batched lines parse in order. No unbounded readline.
- Lease binding: every call re-validates spawning lease; stale fd denied
  with wrong_state (home-side). Missing RPC -> honest prerequisite_missing,
  home lease/limits authoritative, no fallback local tasks.

This module never logs payload credentials; errors carry only code + short
message.
"""
from __future__ import annotations

import codecs
import json
import os
import stat
import threading
import time
from typing import Any

from .errors import ToolDeniedError

RPC_VERSION = "task-rpc-v1"
FD_ENV = "ME_HOME_TASK_RPC_FD"
DEFAULT_FD = 3

RPC_MAX_LINE_BYTES = 64 * 1024
RPC_MAX_BUFFERED_BYTES = 512 * 1024
RPC_MAX_REQUESTS_PER_RUN = 200
RPC_MAX_PARAMS_BYTES = 32 * 1024
RPC_MAX_RESPONSE_BYTES = 128 * 1024
RPC_MAX_PENDING_WRITE_BYTES = 256 * 1024
MAX_METHOD_CHARS = 64


def sanitize_child_env(env: dict | None = None) -> dict:
    """Env for spawned child tools: strip proxy + RPC fd (no inheritance)."""
    base = dict(env) if env is not None else dict(os.environ)
    for k in list(base.keys()):
        if k.upper().endswith("_PROXY"):
            base.pop(k, None)
    base.pop(FD_ENV, None)
    return base


def _valid_rpc_id(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -(2**53) <= v <= 2**53
    if isinstance(v, str):
        return 1 <= len(v) <= 128
    return False


class TaskRpcClient:
    """Bounded duplex JSONL client. Thread-safe for concurrent ToolNode calls."""

    def __init__(self, fd: int, cancelled: threading.Event | None = None,
                 default_timeout: float = 30.0) -> None:
        self._orig_fd = fd
        # Validate: must be socket or fifo, not regular file/dir/device.
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise ToolDeniedError("prerequisite_missing",
                                  f"task RPC fd {fd} unavailable: {exc}") from exc
        mode = st.st_mode
        if not (stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode)):
            raise ToolDeniedError(
                "prerequisite_missing",
                "task RPC fd is not a duplex socket/pipe; "
                "delegation unavailable in this run")
        # Dup so closing our copy never closes the process fd table entry
        # owned by the home spawn; both share the same pipe description.
        try:
            self._fd = os.dup(fd)
        except OSError as exc:
            raise ToolDeniedError("prerequisite_missing",
                                  f"task RPC fd {fd} cannot be duplicated") from exc
        # Our dup must not be inherited by Docker/curl/browser children.
        try:
            os.set_inheritable(self._fd, False)
        except Exception:  # noqa: BLE001
            pass
        self._cancelled = cancelled
        self._default_timeout = default_timeout
        self._lock = threading.Lock()
        self._next_id = 1
        self._count = 0
        self._pending: dict[Any, dict] = {}
        self._pending_lock = threading.Condition(threading.Lock())
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""
        self._buf_bytes = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @classmethod
    def from_env(cls, cancelled: threading.Event | None = None,
                 default_timeout: float = 30.0) -> "TaskRpcClient":
        raw = os.environ.get(FD_ENV, str(DEFAULT_FD))
        try:
            fd = int(str(raw).strip())
        except (ValueError, AttributeError):
            raise ToolDeniedError(
                "prerequisite_missing",
                "delegation unavailable: task RPC fd missing; "
                "home did not provide fd-3 for this run")
        if fd < 3:
            raise ToolDeniedError(
                "prerequisite_missing",
                "delegation unavailable: task RPC fd missing")
        # fd existence checked in __init__; missing fd -> prerequisite_missing.
        try:
            os.fstat(fd)
        except OSError:
            raise ToolDeniedError(
                "prerequisite_missing",
                "delegation unavailable: home did not provide "
                "task RPC for this run (fd-3 absent); "
                "continuing without that child")
        return cls(fd, cancelled, default_timeout)

    def close(self) -> None:
        with self._pending_lock:
            self._closed = True
            for entry in self._pending.values():
                entry["error"] = ("cancelled", "task RPC closed")
                try:
                    entry["event"].set()
                except Exception:  # noqa: BLE001
                    pass
            self._pending.clear()
        try:
            os.close(self._fd)
        except OSError:
            pass

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._count

    def _fail_all(self, code: str, msg: str) -> None:
        with self._pending_lock:
            for entry in list(self._pending.values()):
                entry["error"] = (code, msg)
                try:
                    entry["event"].set()
                except Exception:  # noqa: BLE001
                    pass
            self._pending.clear()

    def _read_loop(self) -> None:
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except OSError:
                self._fail_all("tool_error", "task RPC pipe broken")
                return
            if not chunk:
                # EOF: home closed pipe (run end/cancel/lease takeover).
                self._fail_all("wrong_state",
                               "task RPC pipe closed by home")
                return
            try:
                text = self._decoder.decode(chunk, False)
            except Exception:  # noqa: BLE001
                self._fail_all("bad_request", "task RPC decode error")
                return
            tbytes = len(chunk)
            # Buffered accounting on UTF-8 bytes (approx via encoded len).
            # Use decoded text byte length for cap (home counts UTF-8 bytes).
            added = len(text.encode("utf-8"))
            with self._pending_lock:
                if self._closed:
                    return
                if self._buf_bytes + added > RPC_MAX_BUFFERED_BYTES:
                    self._closed = True
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fail_all("bad_request",
                                   "task RPC buffer flooded without newline")
                    return
                self._buf += text
                self._buf_bytes += added
                # Extract batched lines in order.
                while True:
                    idx = self._buf.find("\n")
                    if idx < 0:
                        break
                    line = self._buf[:idx]
                    rest = self._buf[idx + 1:]
                    self._buf = rest
                    self._buf_bytes = len(rest.encode("utf-8"))
                    if not line.strip():
                        continue
                    if len(line.encode("utf-8")) > RPC_MAX_RESPONSE_BYTES:
                        # Oversize response: correlate id best-effort, drop.
                        try:
                            obj0 = json.loads(line[:4096])
                            oid = obj0.get("id")
                        except Exception:  # noqa: BLE001
                            oid = None
                        if oid is not None and _valid_rpc_id(oid):
                            entry = self._pending.pop(oid, None)
                            if entry is not None:
                                entry["error"] = ("bad_request",
                                                  "rpc response too large")
                                try:
                                    entry["event"].set()
                                except Exception:  # noqa: BLE001
                                    pass
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("rpc") != RPC_VERSION:
                        continue
                    oid = obj.get("id")
                    if not _valid_rpc_id(oid):
                        continue
                    entry = self._pending.pop(oid, None)
                    if entry is None:
                        continue
                    if obj.get("ok") is True:
                        entry["result"] = obj.get("result")
                        try:
                            entry["event"].set()
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        err = obj.get("error") or {}
                        code = str(err.get("code", "bad_request"))[:64]
                        msg = str(err.get("message", "rpc failed"))[:200]
                        entry["error"] = (code, msg)
                        try:
                            entry["event"].set()
                        except Exception:  # noqa: BLE001
                            pass

    def call(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> Any:
        if not isinstance(method, str) or not method or \
                len(method) > MAX_METHOD_CHARS:
            raise ToolDeniedError("invalid_request",
                                  "rpc method must be 1..64 chars")
        if params is None:
            params = {}
        if not isinstance(params, dict) or isinstance(params, list):
            raise ToolDeniedError("invalid_request",
                                  "rpc params must be an object")
        try:
            pbytes = json.dumps(params, ensure_ascii=False).encode("utf-8")
        except Exception:  # noqa: BLE001
            raise ToolDeniedError("invalid_request",
                                  "rpc params not encodable")
        if len(pbytes) > RPC_MAX_PARAMS_BYTES:
            raise ToolDeniedError("invalid_request",
                                  "rpc params too large")
        if self._cancelled is not None and self._cancelled.is_set():
            raise ToolDeniedError("cancelled", "run cancelled")
        with self._lock:
            if self._count >= RPC_MAX_REQUESTS_PER_RUN:
                raise ToolDeniedError(
                    "delegation_denied", "rpc request budget exceeded")
            self._count += 1
            rid = self._next_id
            self._next_id += 1
        req = {"rpc": RPC_VERSION, "id": rid, "method": method,
               "params": params}
        try:
            line = json.dumps(req, ensure_ascii=False) + "\n"
        except Exception:  # noqa: BLE001
            raise ToolDeniedError("invalid_request",
                                  "rpc request not encodable")
        lb = len(line.encode("utf-8"))
        if lb > RPC_MAX_LINE_BYTES:
            raise ToolDeniedError("invalid_request",
                                  "rpc line exceeds 64 KiB")
        ev = threading.Event()
        entry: dict = {"event": ev, "result": None, "error": None}
        with self._pending_lock:
            if self._closed:
                raise ToolDeniedError("wrong_state",
                                      "task RPC pipe closed by home")
            self._pending[rid] = entry
        # Bounded write with lock (serialize concurrent ToolNode calls).
        with self._lock:
            try:
                data = line.encode("utf-8")
                # Pending socket writes cap: best-effort check via ioctl?
                # We bound by refusing to buffer more than cap in one write.
                if len(data) > RPC_MAX_PENDING_WRITE_BYTES:
                    raise ToolDeniedError("invalid_request",
                                          "rpc write too large")
                os.write(self._fd, data)
            except OSError as exc:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise ToolDeniedError("wrong_state",
                                      "task RPC pipe closed") from exc
            except ToolDeniedError:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise
        to = self._default_timeout if timeout is None else timeout
        deadline = time.time() + max(to, 1.0)
        while True:
            if self._cancelled is not None and self._cancelled.is_set():
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise ToolDeniedError("cancelled", "run cancelled")
            remaining = deadline - time.time()
            if remaining <= 0:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise ToolDeniedError("tool_error",
                                      "task RPC call timed out")
            if ev.wait(min(remaining, 0.2)):
                break
        if entry.get("error") is not None:
            code, msg = entry["error"]
            raise ToolDeniedError(code, msg)
        return entry.get("result")
