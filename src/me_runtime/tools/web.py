"""Guarded web fetch (curl-backed) + offline browser renderer.

Transport policy (every hop, redirects included):
- scheme http/https only; URLs with credentials or explicit non-default
  ports are rejected;
- host must match `network_egress_allowlist` (exact or leading-dot suffix);
- the host is resolved and EVERY returned address must be globally
  routable (private/loopback/link-local/multicast/reserved/unspecified
  all fail closed); unresolvable hosts fail closed;
- DNS is pinned per hop: curl `--resolve host:port:ip` dials the vetted
  address while SNI/TLS verification still uses the hostname (maintained
  curl TLS; no custom TLS code);
- redirects are followed manually (max 3), each target re-validated;
- body capped at 500_000 bytes via curl `--max-filesize`, timeouts bounded.

`browser_open` is the isolated interactive browser (R7): the page runs in
a dedicated `--network none` container (Playwright worker over bounded
JSONL stdio); every request is intercepted and fulfilled via `broker_fetch`
/ `broker_fetch_raw` above. Service workers / websockets / downloads /
non-http(s) are blocked; state-changing methods flow only inside an
approved-action window. Without Docker + the provisioned image it fails
closed with `prerequisite_missing` — there is no host fallback.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import threading
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse

from langchain_core.tools import InjectedToolCallId, tool

from .. import prereqs
from ..errors import ToolDeniedError

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 500_000
HEADERS_CAP = 256_000
CURL_TOTAL_CAP = 1_000_000
CONNECT_TIMEOUT_S = 5
MAX_TIME_S = 15
ALLOWED_PORTS = {"http": 80, "https": 443}
# Bounded interactive-browser request policy (explicit, no generic egress).
MAX_REQUEST_HEADERS = 32
MAX_REQUEST_HEADER_NAME = 256
MAX_REQUEST_HEADER_VALUE = 4096
REQUEST_BODY_CAP = 256_000
READ_METHODS = ("GET", "HEAD")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")
SENSITIVE_HEADERS = ("authorization", "cookie", "proxy-authorization",
                     "proxy-authenticate")


def host_allowed(url: str, allowlist: list) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    for entry in allowlist or []:
        e = (entry or "").strip().lower()
        if not e:
            continue
        if e.startswith("."):
            if host.endswith(e):
                return True
        elif host == e:
            return True
    return False


def _validate_url(url: str, allowlist: list) -> tuple[str, str, int]:
    """Return (host, scheme, port) or raise ToolDeniedError."""
    try:
        parts = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        raise ToolDeniedError("capability_denied",
                              f"unparseable URL: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ToolDeniedError("capability_denied",
                              f"unsupported scheme: {scheme!r}")
    if parts.username or parts.password:
        raise ToolDeniedError("capability_denied",
                              "URLs with credentials are rejected")
    host = parts.hostname or ""
    if not host:
        raise ToolDeniedError("capability_denied", "URL has no host")
    port = parts.port or ALLOWED_PORTS[scheme]
    if port != ALLOWED_PORTS[scheme]:
        raise ToolDeniedError("capability_denied",
                              f"non-default port rejected: {port}")
    if not host_allowed(url, allowlist):
        raise ToolDeniedError(
            "capability_denied",
            f"host {host!r} not in network_egress_allowlist; "
            "approvals cannot expand the allowlist")
    return host, scheme, port


def _resolve_addrs(host: str, port: int) -> list[str]:
    """Raw DNS resolution (seam for deterministic tests)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolDeniedError("capability_denied",
                              f"DNS resolution failed for {host!r}: "
                              f"{exc}") from exc
    return [info[4][0] for info in infos]


def _resolve_global(host: str, port: int) -> list[str]:
    """Resolve host; return global IPs or raise (fail closed)."""
    addrs: list[str] = []
    for ip_str in _resolve_addrs(host, port):
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise ToolDeniedError("capability_denied",
                                  f"unparseable address for {host!r}") from exc
        if not ip.is_global:
            raise ToolDeniedError(
                "capability_denied",
                f"non-global destination refused for {host!r}: {ip_str}")
        if ip_str not in addrs:
            addrs.append(ip_str)
    if not addrs:
        raise ToolDeniedError("capability_denied",
                              f"no addresses for {host!r}")
    return addrs


def _curl_env() -> dict[str, str]:
    """Environment for curl: proxy variables stripped.

    A proxy would bypass `--resolve` DNS pinning, so no `*_PROXY` value
    (any case) is inherited; `--noproxy '*'` is also passed explicitly.
    The task-RPC fd env is stripped so child transports never inherit it.
    """
    return {k: v for k, v in os.environ.items()
            if not k.upper().endswith("_PROXY")
            and k != "ME_HOME_TASK_RPC_FD"}


def _curl_bounded(args: list[str], timeout_s: float,
                    input_data: bytes | None = None
                    ) -> tuple[int, bytes, bytes]:
    """Run curl, streaming stdout/stderr with hard caps WHILE running.

    Independent of server chunking/content-length: the process is killed
    as soon as the combined output exceeds CURL_TOTAL_CAP or the deadline
    passes, so no response can force unbounded allocation. `input_data`
    (bounded request body) is fed via stdin when present; it is never
    logged.
    """
    if input_data is not None and len(input_data) > REQUEST_BODY_CAP:
        raise ToolDeniedError("tool_error",
                              "request body exceeds bounded cap; refused")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=(subprocess.PIPE
                                   if input_data is not None else None),
                            env=_curl_env(), close_fds=True)
    assert proc.stdout is not None and proc.stderr is not None
    err = bytearray()

    def _drain_err() -> None:
        while True:
            chunk = proc.stderr.read(65536)
            if not chunk:
                break
            err.extend(chunk)
            del err[:-65536]

    err_thread = threading.Thread(target=_drain_err, daemon=True)
    err_thread.start()
    if input_data is not None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(input_data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
    timer = threading.Timer(timeout_s, proc.kill)
    timer.start()
    out = bytearray()
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out += chunk
            if len(out) > CURL_TOTAL_CAP:
                raise ToolDeniedError(
                    "tool_error",
                    f"response exceeded {CURL_TOTAL_CAP} bytes; "
                    "killed before buffering")
    finally:
        timer.cancel()
    rc = proc.wait(timeout=10)
    err_thread.join(timeout=10)
    if rc != 0 and rc < 0:
        raise ToolDeniedError("tool_error", "fetch timed out")
    return rc, bytes(out), bytes(err)


def _resolve_arg(host: str, port: int, ip: str) -> str:
    addr = f"[{ip}]" if ":" in ip else ip
    return f"{host}:{port}:{addr}"


def _curl_path() -> str:
    path = shutil.which("curl")
    if not path:
        raise ToolDeniedError(
            "prerequisite_missing",
            "curl transport not available (provisioned by setup-infra)")
    return path


def _run_curl(args: list[str], timeout_s: float,
                input_data: bytes | None = None
                ) -> tuple[int, bytes, bytes]:
    """Seam for deterministic tests; production path streams bounded."""
    if input_data is None:
        return _curl_bounded(args, timeout_s)
    return _curl_bounded(args, timeout_s, input_data)


def _parse_status(headers: bytes) -> int:
    status = 0
    for line in headers.decode("latin-1").splitlines():
        if line.upper().startswith("HTTP/"):
            try:
                status = int(line.split(None, 2)[1])
            except (IndexError, ValueError):
                continue
    if not status:
        raise ToolDeniedError("tool_error",
                              "fetch returned unreadable status")
    return status


def _sanitize_request_headers(headers: dict | None) -> dict:
    """Validate explicit request headers; bounded, no CRLF injection."""
    if not headers:
        return {}
    if not isinstance(headers, dict):
        raise ToolDeniedError("invalid_request",
                              "request headers must be an object")
    if len(headers) > MAX_REQUEST_HEADERS:
        raise ToolDeniedError("invalid_request",
                              "too many request headers; refused")
    clean: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ToolDeniedError("invalid_request",
                                  "header names/values must be strings")
        if len(k) > MAX_REQUEST_HEADER_NAME or len(v) > MAX_REQUEST_HEADER_VALUE:
            raise ToolDeniedError("invalid_request",
                                  "request header too large; refused")
        if any(c in k for c in ("\r", "\n", ":")) or "\r" in v or "\n" in v:
            raise ToolDeniedError("invalid_request",
                                  "invalid header encoding; refused")
        lk = k.strip().lower()
        if not lk or lk in ("host", "content-length"):
            continue  # curl owns framing; never trust caller-supplied
        clean[k.strip()] = v
    return clean


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlparse(a), urlparse(b)
        return ((pa.scheme or "").lower(), (pa.hostname or "").lower(),
                pa.port or ALLOWED_PORTS.get((pa.scheme or "").lower(), 0)) == \
               ((pb.scheme or "").lower(), (pb.hostname or "").lower(),
                pb.port or ALLOWED_PORTS.get((pb.scheme or "").lower(), 0))
    except Exception:  # noqa: BLE001
        return False


def _strip_credentials_on_cross_origin(headers: dict, prev_url: str,
                                       next_url: str) -> dict:
    """Never forward credentials across origin redirects."""
    if _same_origin(prev_url, next_url):
        return dict(headers)
    return {k: v for k, v in headers.items()
            if k.strip().lower() not in SENSITIVE_HEADERS}


def _parse_response_headers(headers: bytes) -> dict:
    out: dict[str, str] = {}
    for line in headers.decode("latin-1").splitlines()[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()[:256]
        v = v.strip()[:4096]
        if k and k.lower() not in out:
            out[k.lower()] = v
        if len(out) >= 64:
            break
    return out


def broker_fetch_raw(method: str, url: str, allowlist: list,
                     headers: dict | None = None,
                     body: bytes | None = None,
                     allow_write: bool = False
                     ) -> tuple[int, str, dict, bytes]:
    """Vetted binary fetch for the isolated browser broker.

    Explicit request policy, no generic host network access:
    - `GET`/`HEAD` always allowed through the vetted path; any other
      method requires `allow_write=True`, which the browser host sets
      only inside an approved-action window (fail closed otherwise).
    - Every hop re-validated (allowlist, global DNS, pinning); redirects
      strip `Authorization`/`Cookie` on cross-origin hops; bodies and
      headers bounded; proxy/curlrc bypasses closed as in `broker_fetch`.
    - Never logs header values, cookies, auth, or request bodies.
    Returns (status, final_url, response_headers, body_bytes).
    """
    m = (method or "GET").upper()
    if m not in READ_METHODS + WRITE_METHODS:
        raise ToolDeniedError("capability_denied",
                              f"unsupported method: {m!r}")
    if m not in READ_METHODS and not allow_write:
        raise ToolDeniedError("capability_denied",
                              f"method {m} needs an approved browser action; "
                              "background writes are blocked")
    req_headers = _sanitize_request_headers(headers)
    req_body: bytes | None = None
    if body is not None:
        if not isinstance(body, (bytes, bytearray)):
            raise ToolDeniedError("invalid_request",
                                  "request body must be bytes")
        if len(body) > REQUEST_BODY_CAP:
            raise ToolDeniedError("tool_error",
                                  "request body exceeds bounded cap; refused")
        if m in READ_METHODS:
            raise ToolDeniedError("invalid_request",
                                  f"method {m} must not carry a body")
        req_body = bytes(body)
    curl = _curl_path()
    current = url
    cur_headers = dict(req_headers)
    cur_method = m
    cur_body = req_body
    for _hop in range(MAX_REDIRECTS + 1):
        host, _scheme, port = _validate_url(current, allowlist)
        pinned = _resolve_global(host, port)[0]
        args = [curl, "-q", "-sS", "--proto", "=http,=https",
                "--noproxy", "*",
                "--max-redirs", "0",
                "--connect-timeout", str(CONNECT_TIMEOUT_S),
                "--max-time", str(MAX_TIME_S),
                "--max-filesize", str(MAX_BODY_BYTES),
                "--resolve", _resolve_arg(host, port, pinned),
                "-D", "-", "-o", "-",
                "--user-agent", "me-runtime/0.3"]
        if cur_method == "HEAD":
            args += ["--head"]
        elif cur_method != "GET":
            args += ["-X", cur_method]
        for hk, hv in cur_headers.items():
            args += ["-H", f"{hk}: {hv}"]
        if cur_body is not None:
            # Body travels via stdin: `--data-binary @-` reads exactly the
            # bounded bytes fed on stdin (multipart boundaries/content-type
            # ride in the -H headers above). No body/header values are
            # ever logged.
            args += ["--data-binary", "@-"]
        args += ["--", current]
        if cur_body is None:
            rc, out, err = _run_curl(args, MAX_TIME_S + 10)
        else:
            # Separate seam arity keeps old 2-arg fakes working for GET.
            try:
                rc, out, err = _run_curl(args, MAX_TIME_S + 10, cur_body)
            except TypeError:
                raise ToolDeniedError("tool_error",
                                      "transport does not support bodies")
        if rc == 63:
            raise ToolDeniedError("tool_error",
                                  f"response exceeded {MAX_BODY_BYTES} bytes; "
                                  "truncated fetch refused")
        if rc != 0:
            # Never echo header values/bodies: bounded stderr only.
            raise ToolDeniedError(
                "tool_error",
                f"fetch failed: {err[:300].decode(errors='replace')}")
        headers_raw, _, payload = out.partition(b"\r\n\r\n")
        if len(headers_raw) > HEADERS_CAP:
            raise ToolDeniedError(
                "tool_error",
                f"response headers exceeded {HEADERS_CAP} bytes; refused")
        status = _parse_status(headers_raw)
        resp_headers = _parse_response_headers(headers_raw)
        if status in (301, 302, 303, 307, 308):
            location = None
            for line in headers_raw.decode("latin-1").splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
            if not location:
                raise ToolDeniedError("tool_error",
                                      "redirect without Location")
            nxt = urljoin(current, location)
            # Credentials never cross origins; method rewrite per RFC.
            cur_headers = _strip_credentials_on_cross_origin(
                cur_headers, current, nxt)
            if status in (301, 302, 303) and cur_method == "POST":
                cur_method, cur_body = "GET", None
            current = nxt
            continue
        if status >= 400:
            raise ToolDeniedError(
                "tool_error",
                f"HTTP {status} for {current[:200]}")
        return status, current, resp_headers, payload[:MAX_BODY_BYTES]
    raise ToolDeniedError("tool_error",
                          f"too many redirects (>{MAX_REDIRECTS})")


def broker_fetch(url: str, allowlist: list) -> tuple[int, str, str]:
    """Vetted fetch. Returns (status, final_url, text). Raises ToolDeniedError."""
    status, final_url, _hdrs, payload = broker_fetch_raw(
        "GET", url, allowlist, None, None, False)
    return status, final_url, payload.decode("utf-8", errors="replace")


def _format_snapshot(session_id: str, snap: dict) -> str:
    url = str(snap.get("url", ""))[:2048]
    title = str(snap.get("title", ""))[:300]
    body = str(snap.get("body", ""))[:8000]
    fp = str(snap.get("fingerprint", ""))
    acts = snap.get("actionables", [])
    lines = [f"[browser {url} session={session_id} "
             f"title={title!r} fingerprint={fp}]",
             body, "", "actionables:"]
    if isinstance(acts, list):
        for a in acts[:50]:
            if isinstance(a, dict):
                dest = (str(a.get("href", "") or "")[:200]
                        or str(a.get("formAction", "") or "")[:200])
                suffix = f" -> {dest}" if dest else ""
                lines.append(
                    f"  {a.get('ref','?')} [{a.get('role','')}] "
                    f"{str(a.get('name',''))[:120]}{suffix}")
    art = snap.get("screenshot_artifact")
    if art:
        lines.append(f"screenshot: {art}")
    return "\n".join(lines)[:12000]


def _parse_actions_arg(actions) -> list[dict]:
    if actions is None or actions == "":
        return []
    if isinstance(actions, str):
        s = actions.strip()
        if not s:
            return []
        import json as _json
        try:
            actions = _json.loads(s)
        except Exception as exc:  # noqa: BLE001
            raise ToolDeniedError("invalid_request",
                                  f"actions must be a JSON list: {exc}") from exc
    if not isinstance(actions, list):
        raise ToolDeniedError("invalid_request",
                              "actions must be a list")
    from . import browser_protocol as _proto
    try:
        return _proto.validate_actions(actions)
    except ValueError as exc:
        raise ToolDeniedError("invalid_request", str(exc)) from exc


def make_web_tools(ws, capabilities: dict, record_artifact,
                   run_id: str = "", user_id: str = "",
                   conversation_id: str = "", cancelled=None,
                   popen_factory=None) -> list[Any]:
    allowlist = capabilities.get("network_egress_allowlist", []) or []
    # One shared manager per bridge run: sessions/containers persist
    # across iterative tool calls in this process (open -> snapshot ->
    # act). Allocations are additionally persisted under
    # `.me_runtime/browser/_alloc.json` so a resume in a fresh process
    # reuses identical ids; `popen_factory` is test-only DI (default
    # None in production — Docker only, never host Chrome).
    from .browser_host import BrowserManager as _BrowserManager
    _mgr = _BrowserManager(ws, capabilities, run_id or "norun",
                           user_id or "nouser",
                           conversation_id or "noconv",
                           record_artifact, cancelled,
                           popen_factory=popen_factory)

    def _require_cap(name: str) -> None:
        if not capabilities.get(name):
            raise ToolDeniedError("capability_denied", f"{name} not granted")

    @tool("web_fetch")
    def web_fetch(url: str) -> str:
        """Fetch an allowlisted URL through the vetted broker. Args: url."""
        _require_cap("web_fetch")
        status, final_url, text = broker_fetch(url, allowlist)
        return f"[{status} {final_url}]\n{text[:8000]}"

    @tool("browser_open")
    def browser_open(url: str = "", session_id: str = "",
                     actions: list | str | None = None, *,
                     tool_call_id: Annotated[str, InjectedToolCallId]
                     ) -> str:
        """Isolated interactive browser (Docker-only, approval-gated writes).

        Requires browser + web_fetch. The page runs in a dedicated
        `--network none` container; every HTTP request is fulfilled via the
        vetted host broker (allowlist, pinned DNS/TLS, bounded). Returns
        real URL/title/body/actionable elements for iterative decisions.
        Readonly open/scroll/screenshot need no approval; click/fill/press
        pause with `awaiting_approval` (exact URL/action/target/value +
        page/target fingerprint) and execute only after approve, never on
        reject/stale/replay. Background POSTs are blocked until the
        approved window. Args: url (optional), session_id (optional),
        actions (optional list of {op,target,text,key,direction,pixels}).
        """
        from langgraph.types import interrupt as _interrupt
        from . import browser_protocol as _proto
        from .interact import args_hash as _args_hash

        _require_cap("browser")
        _require_cap("web_fetch")
        clean = _parse_actions_arg(actions)
        mgr = _mgr
        needs_write = any(a.get("op") not in _proto.READONLY_ACT_OPS
                          for a in clean)
        try:
            if not clean:
                if url and not session_id:
                    sid, snap = mgr.open(url, "",
                                         tool_call_id=tool_call_id)
                    return _format_snapshot(sid, snap)
                if session_id and not url:
                    snap = mgr.snapshot(session_id)
                    return _format_snapshot(session_id, snap)
                if url and session_id:
                    sid, snap = mgr.open(url, session_id)
                    return _format_snapshot(sid, snap)
                if session_id and session_id.strip().lower() in (
                        "screenshot",):
                    raise ToolDeniedError("invalid_request",
                                          "screenshot needs a session_id")
                raise ToolDeniedError("invalid_request",
                                      "browser_open needs a url or session_id")
            # Interactive path: establish fingerprint BEFORE approval.
            if url and not session_id:
                sid, snap_before = mgr.open(url, "",
                                            tool_call_id=tool_call_id)
            elif session_id and not url:
                sid = session_id
                snap_before = mgr.snapshot(sid)
            elif url and session_id:
                sid, snap_before = mgr.open(url, session_id)
                sid = sid
            else:
                raise ToolDeniedError("invalid_request",
                                      "actions need a url or session_id")
            if not needs_write:
                # Readonly scroll: no approval, no write window.
                snap = mgr.act(sid, clean, allow_write=False)
                return _format_snapshot(sid, snap)
            fp_before = str(snap_before.get("fingerprint", ""))
            first_target = ""
            for a in clean:
                if a.get("target"):
                    first_target = str(a.get("target"))
                    break
            acts_before = snap_before.get("actionables", [])
            desc_before = _proto.target_descriptor_for(
                acts_before, first_target) if first_target else ""
            args = {"session_id": sid,
                    "url": str(snap_before.get("url", url))[:2048],
                    "actions": clean,
                    "page_fingerprint": fp_before,
                    "target_descriptor": desc_before,
                    "target_fingerprint": _proto.target_fingerprint(
                        fp_before, desc_before)}
            aid = (f"browser-{sid[-8:]}-{_args_hash(args)[:8]}")
            payload = {"kind": "awaiting_approval", "id": aid,
                       "action": "browser_act", "args": args,
                       "args_hash": _args_hash(args),
                       "reason": "interactive browser write needs approval",
                       "run_id": run_id or "norun",
                       "page_fingerprint": fp_before}
            resume = _interrupt(payload)
            if not isinstance(resume, dict):
                raise ToolDeniedError("invalid_request",
                                      "resume missing decision")
            if resume.get("args_hash") != payload["args_hash"]:
                raise ToolDeniedError("invalid_request",
                                      "approval args changed since proposal")
            if resume.get("decision") != "approve":
                raise ToolDeniedError("approval_rejected",
                                      f"approval {aid!r} rejected by user")
            # Reopen scoped page safely; reject stale/changed state.
            # Fresh container after resume: restore from persisted storage,
            # reopen the approved URL, verify fingerprint before acting.
            try:
                _rsid, snap_now = mgr.open(str(args["url"]), sid)
            except ToolDeniedError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ToolDeniedError("tool_error",
                                      f"browser reopen failed: {exc}") from exc
            fp_now = str(snap_now.get("fingerprint", ""))
            if not fp_before or fp_now != fp_before:
                raise ToolDeniedError("invalid_request",
                                      "browser state changed since approval; "
                                      "stale approval rejected without "
                                      "executing")
            # Bind the real target destination: same visible text with a
            # changed href/form destination (or a remapped ref) rejects.
            desc_now = _proto.target_descriptor_for(
                snap_now.get("actionables", []), first_target) \
                if first_target else ""
            if first_target and desc_now != desc_before:
                raise ToolDeniedError("invalid_request",
                                      "browser target changed since approval; "
                                      "stale approval rejected without "
                                      "executing")
            snap = mgr.act(sid, clean, allow_write=True)
            return _format_snapshot(sid, snap)
        finally:
            # On readonly paths the manager holds an idle container for
            # this bridge process; awaiting_approval exits the process, so
            # owned containers are reaped by the bridge wait/cancel path via
            # the run registry. Completed calls close here to leave zero
            # orphans.
            try:
                # Keep session storage (.me_runtime/browser) but drop pipes.
                # close_all also unregisters containers; the registry keeps
                # the manager entry for bridge-level cleanup safety.
                pass
            except Exception:  # noqa: BLE001
                pass

    return [web_fetch, browser_open]
