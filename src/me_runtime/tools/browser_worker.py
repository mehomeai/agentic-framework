"""Container Playwright worker (browser-worker-v1). STANDALONE.

Runs ONLY inside the dedicated Docker container (`--network none`): it has
no NIC, so page JS/WebRTC cannot reach home/LAN. Every HTTP(S) request is
intercepted via a Playwright route and fulfilled through the host vetted
broker over stdio JSONL. No arbitrary eval tool is exposed; the only DOM
snapshot script is fixed inside this file and takes no model input.

Stdio protocol (one JSON object per line, flushed):
  host -> worker: {"v":1,"id":N,"op":"open","url":...,"storage_state_b64"?...}
                  {"v":1,"id":N,"op":"snapshot"}
                  {"v":1,"id":N,"op":"act","actions":[...]}
                  {"v":1,"id":N,"op":"screenshot"}
                  {"v":1,"id":N,"op":"close"}
  worker -> host: {"v":1,"id":N,"type":"fetch","fetch_id":..,"method":..,
                   "url":..,"headers":..,"body_b64":..}   (request event)
  host -> worker: {"v":1,"id":N,"type":"fetch_response","fetch_id":..,
                   "status":int,"headers":dict,"body_b64":str}
                   or {"v":1,"id":N,"type":"fetch_error","fetch_id":..,
                       "error":str}
  worker -> host: {"v":1,"id":N,"type":"result","ok":bool,"payload":dict,
                   "error":str}

Service workers / websockets / downloads are blocked; only http(s) GET/HEAD
document+subresource fetches via the host broker are fulfilled (POST only
when the host explicitly allows an approved-action window).
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import os

MAX_FRAME = 2_000_000
FETCH_BODY_CAP = 500_000

_snapshot_js = """() => {
  const els = Array.from(document.querySelectorAll(
    'a,button,input,select,textarea,[role=button],[role=link],[role=textbox]'));
  const out = [];
  let n = 0;
  const canon = (v, lim) => String(v == null ? '' : v).trim().slice(0, lim);
  for (const el of els) {
    if (n >= 50) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = canon(el.tagName, 16).toLowerCase();
    const name = canon(el.innerText || el.getAttribute('aria-label')
                  || el.getAttribute('name') || el.tagName || '', 120);
    const role = canon(el.getAttribute('role') || el.tagName || '', 32)
                  .toLowerCase();
    const inputType = canon(el.getAttribute('type') || '', 32);
    let href = '';
    let formAction = '';
    let formMethod = '';
    try {
      const a = el.closest ? el.closest('a[href]') : null;
      const link = (tag === 'a') ? el : a;
      if (link && link.href != null) href = canon(link.getAttribute('href') || link.href || '', 512);
    } catch (e) {}
    try {
      const f = el.form || (el.closest ? el.closest('form') : null);
      if (f) {
        formAction = canon(f.getAttribute('action') || f.action || '', 512);
        formMethod = canon(f.getAttribute('method') || f.method || '', 16).toUpperCase();
      }
      const fa = el.getAttribute ? el.getAttribute('formaction') : null;
      if (fa) formAction = canon(fa, 512);
    } catch (e) {}
    out.push({ref: 'e' + n, role, name, href, formAction, formMethod,
              inputType, tag, itype: inputType});
    n++;
  }
  const body = (document.body ? document.body.innerText : '').slice(0, 8000);
  return {title: document.title.slice(0, 300), body, actionables: out,
          url: location.href};
}"""


def _write(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_FRAME:
        # Never emit an oversized frame: report truncation instead.
        obj = {"v": 1, "id": obj.get("id"),
               "type": "result", "ok": False,
               "error": "result exceeds bounded frame cap"}
        line = json.dumps(obj)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _readline() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    if len(line.encode("utf-8")) > MAX_FRAME + 1024:
        raise ValueError("inbound frame too large")
    return json.loads(line)


class Worker:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._fetch_seq = 0
        self._pending_fetch: dict[str, dict] = {}

    def _ensure_page(self, storage_state_b64: str | None = None):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu"])
        except Exception:
            # System-Chrome fallback is TEST-HARNESS ONLY (host synthetic
            # smoke); the production container image ships its own browser.
            exe = os.environ.get("ME_BROWSER_TEST_EXECUTABLE", "")
            if not exe:
                raise
            self._browser = self._pw.chromium.launch(
                headless=True, executable_path=exe,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx_kwargs: dict = {"service_workers": "block",
                            "accept_downloads": False}
        if storage_state_b64:
            raw = base64.b64decode(storage_state_b64.encode("ascii"))
            if len(raw) > 256_000:
                raise ValueError("storage state too large")
            tmp = tempfile.NamedTemporaryFile(
                suffix=".json", delete=False, dir="/tmp")
            try:
                tmp.write(raw)
                tmp.close()
                ctx_kwargs["storage_state"] = tmp.name
                self._ctx = self._browser.new_context(**ctx_kwargs)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        else:
            self._ctx = self._browser.new_context(**ctx_kwargs)
        self._page = self._ctx.new_page()
        self._page.route("**", self._route)
        return self._page

    def _route(self, route) -> None:
        req = route.request
        url = req.url or ""
        if url.startswith(("ws:", "wss:", "ftp:", "file:", "data:")):
            try:
                route.abort()
            except Exception:  # noqa: BLE001
                pass
            return
        if req.service_worker:
            try:
                route.abort()
            except Exception:  # noqa: BLE001
                pass
            return
        method = (req.method or "GET").upper()
        headers = {}
        try:
            headers = dict(req.headers or {})
        except Exception:  # noqa: BLE001
            headers = {}
        # Bound headers sent to the host; values stay opaque to logs.
        safe_headers = {}
        for k, v in list(headers.items())[:32]:
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            safe_headers[k[:256]] = v[:4096]
        body_b64 = ""
        post = req.post_data_buffer
        if post:
            body_b64 = base64.b64encode(bytes(post)[:256_000]).decode("ascii")
        self._fetch_seq += 1
        fid = f"f{self._fetch_seq}"
        cmd_id = getattr(self, "_active_cmd", 0)
        _write({"v": 1, "id": cmd_id, "type": "fetch", "fetch_id": fid,
                "method": method, "url": url[:2048],
                "headers": safe_headers, "body_b64": body_b64})
        # Blocking wait for the host broker response on stdin.
        while True:
            msg = _readline()
            if msg is None:
                try:
                    route.abort()
                except Exception:  # noqa: BLE001
                    pass
                return
            if (msg.get("type") == "fetch_response"
                    and msg.get("fetch_id") == fid
                    and msg.get("id") == cmd_id):
                try:
                    status = int(msg.get("status", 200))
                    rheaders = msg.get("headers", {})
                    if not isinstance(rheaders, dict):
                        rheaders = {}
                    body = base64.b64decode(
                        (msg.get("body_b64", "") or "").encode("ascii"))
                    if len(body) > FETCH_BODY_CAP:
                        route.abort()
                        return
                    route.fulfill(status=status, headers=dict(
                        list(rheaders.items())[:32]), body=body)
                except Exception:  # noqa: BLE001
                    try:
                        route.abort()
                    except Exception:  # noqa: BLE001
                        pass
                return
            if (msg.get("type") == "fetch_error"
                    and msg.get("fetch_id") == fid):
                try:
                    route.abort()
                except Exception:  # noqa: BLE001
                    pass
                return
            # Ignore unrelated lines while awaiting our fetch response.

    def _canon_desc(self, a: dict) -> str:
        if not isinstance(a, dict):
            return ""
        def s(k, lim):
            v = a.get(k, "")
            return str(v or "")[:lim]
        return "|".join([
            s("ref", 64), s("role", 32).lower(), s("name", 120),
            s("href", 512), s("formAction", 512),
            s("formMethod", 16).upper(),
            s("inputType", 32) or s("itype", 32), s("tag", 16).lower()])

    def _snapshot(self, page) -> dict:
        try:
            data = page.evaluate(_snapshot_js)
        except Exception:
            data = {"title": "", "body": "", "actionables": [],
                    "url": ""}
        if not isinstance(data, dict):
            data = {"title": "", "body": "", "actionables": [],
                    "url": ""}
        url = str(data.get("url") or page.url or "")[:2048]
        title = str(data.get("title") or "")[:300]
        body = str(data.get("body") or "")[:8000]
        acts = data.get("actionables", [])
        if not isinstance(acts, list):
            acts = []
        h = hashlib.sha256()
        h.update((url + "\n" + title + "\n").encode())
        h.update(hashlib.sha256(body.encode()).digest())
        h.update(b"\n")
        for a in acts[:50]:
            if isinstance(a, dict):
                h.update(self._canon_desc(a).encode(errors="replace"))
                h.update(b"\x1f")
        fp = h.hexdigest()[:32]
        storage_b64 = ""
        try:
            if self._ctx is not None:
                st = self._ctx.storage_state()
                raw = json.dumps(st).encode("utf-8")
                if len(raw) <= 256_000:
                    storage_b64 = base64.b64encode(raw).decode("ascii")
        except Exception:  # noqa: BLE001
            storage_b64 = ""
        return {"url": url, "title": title, "body": body,
                "actionables": acts[:50], "fingerprint": fp,
                "storage_state_b64": storage_b64}

    def _resolve_index(self, ref: str) -> int:
        if not ref.startswith("e"):
            raise ValueError(f"unknown target {ref!r}")
        try:
            return int(ref[1:])
        except ValueError:
            raise ValueError(f"unknown target {ref!r}") from None

    def _do_act(self, page, action: dict) -> str:
        op = action.get("op")
        if op == "scroll":
            direction = action.get("direction", "down")
            pixels = int(action.get("pixels", 800))
            dy = pixels if direction == "down" else -pixels
            page.evaluate(
                "(dy) => window.scrollBy(0, dy)", dy)
            return f"scrolled {direction} {pixels}px"
        if op == "click":
            idx = self._resolve_index(action.get("target", ""))
            els = page.query_selector_all(
                "a,button,input,select,textarea,"
                "[role=button],[role=link],[role=textbox]")
            vis = [e for e in els if e.is_visible()]
            if idx < 0 or idx >= len(vis):
                raise ValueError("target no longer present; page changed")
            vis[idx].click(timeout=15000)
            return f"clicked {action.get('target')}"
        if op == "fill":
            idx = self._resolve_index(action.get("target", ""))
            els = page.query_selector_all(
                "input,textarea,select,[role=textbox]")
            vis = [e for e in els if e.is_visible()]
            if idx < 0 or idx >= len(vis):
                raise ValueError("target no longer present; page changed")
            vis[idx].fill(str(action.get("text", ""))[:4000], timeout=15000)
            return f"filled {action.get('target')}"
        if op == "press":
            key = str(action.get("key", ""))[:64]
            target = action.get("target", "")
            if target:
                idx = self._resolve_index(target)
                els = page.query_selector_all(
                    "input,textarea,select,[role=textbox],a,button")
                vis = [e for e in els if e.is_visible()]
                if idx < 0 or idx >= len(vis):
                    raise ValueError("target no longer present")
                vis[idx].press(key, timeout=15000)
            else:
                page.keyboard.press(key)
            return f"pressed {key}"
        raise ValueError(f"unsupported op {op!r}")

    def handle(self, cmd: dict) -> dict:
        op = cmd.get("op")
        if op == "open":
            url = str(cmd.get("url", ""))[:2048]
            if not url:
                raise ValueError("open needs a url")
            page = self._ensure_page(cmd.get("storage_state_b64"))
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            snap = self._snapshot(page)
            shot = b""
            try:
                shot = page.screenshot(timeout=15000)
            except Exception:  # noqa: BLE001
                shot = b""
            if len(shot) > 1_000_000:
                shot = b""
            snap["screenshot_b64"] = base64.b64encode(shot).decode("ascii")
            return snap
        if op == "snapshot":
            page = self._ensure_page()
            return self._snapshot(page)
        if op == "act":
            page = self._ensure_page()
            actions = cmd.get("actions", [])
            if not isinstance(actions, list) or len(actions) > 8:
                raise ValueError("bad actions")
            results = []
            for a in actions:
                results.append(self._do_act(page, a))
                try:
                    page.wait_for_load_state("domcontentloaded",
                                             timeout=3000)
                except Exception:  # noqa: BLE001
                    pass
            snap = self._snapshot(page)
            snap["action_results"] = results
            return snap
        if op == "screenshot":
            page = self._ensure_page()
            shot = page.screenshot(timeout=15000)
            if len(shot) > 1_000_000:
                raise ValueError("screenshot exceeds bounded cap")
            snap = self._snapshot(page)
            snap["screenshot_b64"] = base64.b64encode(shot).decode("ascii")
            return snap
        if op == "close":
            self.close()
            return {"closed": True}
        raise ValueError(f"unknown op {op!r}")

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._ctx = self._page = None


def main() -> int:
    worker = Worker()
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:  # noqa: BLE001
            break
        if not line:
            break
        try:
            cmd = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(cmd, dict):
            continue
        cmd_id = cmd.get("id", 0)
        worker._active_cmd = cmd_id
        try:
            payload = worker.handle(cmd)
            _write({"v": 1, "id": cmd_id, "type": "result",
                    "ok": True, "payload": payload})
        except Exception as exc:  # noqa: BLE001
            _write({"v": 1, "id": cmd_id, "type": "result",
                    "ok": False, "error": str(exc)[:500]})
        if cmd.get("op") == "close":
            break
    worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
