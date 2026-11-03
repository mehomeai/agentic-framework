"""Isolated-browser JSONL protocol (browser-worker-v1).

Host <-> container worker speak bounded JSONL over stdio (`docker run -i`).
The container has no NIC; every page HTTP request is intercepted in the
worker and fulfilled via the host vetted broker. No arbitrary eval, no CDP,
no host mounts; frames are size-bounded and actions per command are capped.
"""
from __future__ import annotations

import base64
import hashlib
import json

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2_000_000
MAX_ACTIONS_PER_CMD = 8
MAX_URL_LEN = 2048
MAX_REF_LEN = 64
MAX_TEXT_LEN = 4000
MAX_KEY_LEN = 64
MAX_BODY_TEXT = 8000
MAX_ACTIONABLES = 50
MAX_STORAGE_STATE_BYTES = 256_000
SCREENSHOT_CAP_BYTES = 1_000_000

HOST_OPS = ("open", "snapshot", "act", "screenshot", "close")
ACT_OPS = ("click", "fill", "press", "scroll")
READONLY_ACT_OPS = ("scroll",)


def encode_frame(obj: dict) -> str:
    line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds bounded cap")
    return line


def decode_frame(line: str) -> dict:
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("inbound frame exceeds bounded cap")
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("frame must be an object")
    return obj


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("url must be a non-empty string")
    if len(url) > MAX_URL_LEN:
        raise ValueError("url too long; refused")
    return url


def validate_actions(actions) -> list[dict]:
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    if len(actions) > MAX_ACTIONS_PER_CMD:
        raise ValueError("too many actions; refused")
    clean: list[dict] = []
    for a in actions:
        if not isinstance(a, dict):
            raise ValueError("each action must be an object")
        op = a.get("op")
        if op not in ACT_OPS:
            raise ValueError(f"unsupported action op: {op!r}")
        target = a.get("target", "")
        if op in ("click", "fill"):
            if not isinstance(target, str) or not target:
                raise ValueError(f"{op} needs a target ref")
            if len(target) > MAX_REF_LEN:
                raise ValueError("target ref too long")
        if op == "fill":
            text = a.get("text", "")
            if not isinstance(text, str):
                raise ValueError("fill text must be a string")
            if len(text) > MAX_TEXT_LEN:
                raise ValueError("fill text too long; refused")
            clean.append({"op": op, "target": target, "text": text})
        elif op == "click":
            clean.append({"op": op, "target": target})
        elif op == "press":
            key = a.get("key", "")
            if not isinstance(key, str) or not key:
                raise ValueError("press needs a key")
            if len(key) > MAX_KEY_LEN:
                raise ValueError("press key too long")
            t = a.get("target", "")
            if t and (not isinstance(t, str) or len(t) > MAX_REF_LEN):
                raise ValueError("bad press target")
            clean.append({"op": op, "key": key, "target": t})
        elif op == "scroll":
            direction = a.get("direction", "down")
            if direction not in ("up", "down"):
                raise ValueError("scroll direction must be up/down")
            try:
                pixels = int(a.get("pixels", 800))
            except (TypeError, ValueError):
                raise ValueError("scroll pixels must be an integer")
            pixels = max(100, min(pixels, 4000))
            clean.append({"op": op, "direction": direction,
                          "pixels": pixels})
    return clean


def canonical_target_descriptor(a: dict) -> str:
    """Bounded canonical descriptor for one actionable (no raw secrets).

    Covers the real destination, not just the visible text/ref: role, name,
    href (links), form action/method (buttons/inputs), input type and tag.
    User-typed values are never included (only the pre-action destination).
    """
    if not isinstance(a, dict):
        return ""
    parts = [
        str(a.get("ref", ""))[:64],
        str(a.get("role", ""))[:32].lower(),
        str(a.get("name", ""))[:120],
        str(a.get("href", ""))[:512],
        str(a.get("formAction", ""))[:512],
        str(a.get("formMethod", ""))[:16].upper(),
        str(a.get("inputType", a.get("itype", "")))[:32],
        str(a.get("tag", ""))[:16].lower(),
    ]
    return "|".join(parts)


def find_actionable(actionables, ref: str) -> dict | None:
    if not isinstance(actionables, list) or not ref:
        return None
    for a in actionables:
        if isinstance(a, dict) and a.get("ref") == ref:
            return a
    return None


def target_descriptor_for(actionables, ref: str) -> str:
    found = find_actionable(actionables, ref)
    if found is None:
        return ""
    return canonical_target_descriptor(found)


def page_fingerprint(url: str, title: str, body_text: str,
                     actionables=None) -> str:
    """Page fingerprint bound to real destinations, not just visible text.

    Includes the canonical descriptor of every actionable (role/name/href/
    formAction/formMethod/inputType/tag) so a same-text link whose href (or
    a button whose form destination) changed yields a different
    fingerprint. Only hashes are stored — never raw secrets.
    """
    h = hashlib.sha256()
    h.update((url or "").encode("utf-8", errors="replace"))
    h.update(b"\n")
    h.update((title or "").encode("utf-8", errors="replace"))
    h.update(b"\n")
    h.update(hashlib.sha256(
        (body_text or "").encode("utf-8", errors="replace")).digest())
    h.update(b"\n")
    if isinstance(actionables, list):
        for a in actionables[:MAX_ACTIONABLES]:
            if isinstance(a, dict):
                h.update(canonical_target_descriptor(a).encode(
                    "utf-8", errors="replace"))
                h.update(b"\x1f")
    return h.hexdigest()[:32]


def target_fingerprint(page_fp: str, target_descriptor: str) -> str:
    h = hashlib.sha256()
    h.update((page_fp or "").encode())
    h.update(b"\n")
    h.update((target_descriptor or "").encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(s: str, cap: int = MAX_FRAME_BYTES) -> bytes:
    if not isinstance(s, str):
        raise ValueError("base64 payload must be a string")
    if len(s) > cap * 4 // 3 + 16:
        raise ValueError("base64 payload exceeds cap")
    try:
        raw = base64.b64decode(s.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad base64: {exc}") from exc
    if len(raw) > cap:
        raise ValueError("decoded payload exceeds cap")
    return raw
