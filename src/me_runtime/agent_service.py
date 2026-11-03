"""Native upstream agent service packaging (OpenBot agent-langgraph-agui).

Production path: the pinned OpenBot container serves AG-UI; the OpenBot
server owns `/api/agent-tools/call`; home-owned self-hosted Intelligence
persists threads (see results/runtime-wire-contract.json). This module
stages a reproducible, source-pinned build context and validates operator
configuration — it does NOT reimplement the agent and owns no run transport.

Image recipe (upstream `agent-langgraph-agui/Dockerfile` @ pinned SHA
+ BUILD-001: COPY constraints-me.txt, `pip install -r requirements.txt
-c constraints-me.txt` so the lock is actually consumed):
  FROM python:3.12-slim; pip install requirements (-c constraints);
  COPY agent-langgraph-agui/src ./src; COPY shared/model_providers.py shared/model-providers.json /shared/
  ENV PORT=4206; EXPOSE 4206
  CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "4206"]

`build_context()` stages that recipe from in-repo vendored sources
(provenance-verified against MANIFEST.json BEFORE copying), plus a
constraints file with the coherent resolved pins verified in this
repo's venv.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

AGENT_PIN = "6bfee780c8e8d05141375ea55ebde33eefb3dbc0"
AGENT_REPO = "CopilotKit/OpenBot"
AGENT_DIR = "agent-langgraph-agui"
AGENT_PORT = 4206
# add_langgraph_fastapi_endpoint(app, agent, path="/"): POST / is AG-UI.
AGENT_AGUI_PATH = "/"
AGENT_HEALTH_PATH = "/health"
TOKEN_HEADER = "x-openbot-agent-token"

REQUIRED_TOKENS = ("MANAGED_AGENT_TOKEN", "AGENT_TOOL_TOKEN")

# Base image is a moving tag upstream; the digest is captured at build
# time by whoever runs `docker build` (A/CI) and recorded in the runtime
# wire contract. Never baked here as a stale digest.
BASE_IMAGE = "python:3.12-slim"

# Upstream requirements @ pin, verbatim (only ag-ui-langgraph is pinned
# there; hash anchored in MANIFEST.json `generated.requirements_txt_sha256`).
# Coherent resolved pins are the versions verified in this repo's venv
# (see RESOLVED_PINS); unlisted rows (uvicorn, python-multipart,
# langchain-google-genai) resolve at image build time and are captured by
# Linux CI (`pip freeze` in the built image must match every constrained
# row and record the trio).
UPSTREAM_REQUIREMENTS = """ag-ui-langgraph==0.0.45
langgraph
langchain
langchain-openai
langchain-anthropic
langchain-google-genai
fastapi
python-multipart
uvicorn[standard]
httpx>=0.28,<1
"""

# Resolved pins verified installed in this repo's venv on 2026-11-03.
# Written into the build context as constraints-me.txt so image builds
# resolve the same framework versions the protocol tests ran against.
# Every row here MUST be installed by the image requirements closure;
# Linux CI asserts each one from `pip freeze` in the built image.
RESOLVED_PINS = (
    "ag-ui-langgraph==0.0.45",
    "ag-ui-protocol==1.0.0",
    "langgraph==1.2.19",
    "langgraph-checkpoint==4.3.1",
    "langgraph-prebuilt==1.1.1",
    "langgraph-sdk==0.4.11",
    "langchain==1.4.8",
    "langchain-core==1.7.4",
    "langchain-openai==1.7.3",
    "langchain-anthropic==1.7.9",
    "fastapi==0.162.0",
    "starlette==1.10.0",
    "httpx==0.28.1",
    "pydantic==2.14.2",
)

# Dev/test-only pins: kept in requirements.txt for this repo's venv
# (devmode SQLite checkpointer, legacy bridge) but deliberately NOT in
# constraints-me.txt — no image requirement installs them and CI must not
# demand them from the image freeze.
DEV_ONLY_PINS = (
    "langgraph-checkpoint-sqlite==3.1.3",
)

# Vendored source files copied into the build context (hash-verified).
VENDOR_SRC_FILES = ("main.py", "tool_runtime.py", "parallel_tools.py",
                    "chatgpt_store.py")
VENDOR_SHARED_FILES = ("model_providers.py", "model-providers.json")

# Model patches applied on top of the pinned source (empty = unmodified
# upstream graph; env-only model hook, no code patch).
MODEL_PATCHES: tuple = ()


def _vendor_dir() -> Path:
    return Path(__file__).resolve().parent / "upstream" / "vendor"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pinned_dockerfile() -> str:
    """Exact upstream Dockerfile @ pin (verified against audit source)."""
    return """# LangGraph, as a Bot. Python rather than Bun because that is what the integration is published in,
# and the protocol is the only thing a Bot has to share with the others.
FROM python:3.12-slim

WORKDIR /app

# Dependencies before source, so editing the Bot does not re-resolve the framework.
COPY agent-langgraph-agui/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent-langgraph-agui/src ./src
COPY shared/model_providers.py shared/model-providers.json /shared/

ENV PORT=4206
EXPOSE 4206
# 0.0.0.0, not `::`. uvicorn binds `::` as IPv6-only, with no v4-mapped addresses, so a container
# started that way refuses 127.0.0.1: the Compose healthcheck never passes and the server cannot
# reach the Bot. Measured, not assumed. Inside a container this is the container's own namespace,
# and Compose is what decides which host addresses it is published on.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "4206"]
"""


def patched_dockerfile() -> str:
    """Generated image Dockerfile: base recipe + BUILD-001 only.

    BUILD-001 (constraints lock actually consumed): COPY
    `constraints-me.txt` into the image and apply it with
    `pip install ... -c constraints-me.txt`, so the resolved pins are
    enforced at image build instead of floating. The upstream graph,
    imports, entrypoint, and CMD are byte-identical to the base recipe.
    """
    base = pinned_dockerfile()
    old = ("COPY agent-langgraph-agui/requirements.txt ./\n"
           "RUN pip install --no-cache-dir -r requirements.txt\n")
    new = ("COPY agent-langgraph-agui/requirements.txt ./\n"
           "COPY constraints-me.txt ./\n"
           "RUN pip install --no-cache-dir -r requirements.txt "
           "-c constraints-me.txt\n")
    assert old in base, "base recipe drifted; re-record BUILD-001"
    return base.replace(old, new)


def _manifest() -> dict:
    """Load the in-repo trusted vendor manifest (full 64-hex hashes)."""
    import json

    return json.loads(
        (Path(__file__).resolve().parent / "upstream" / "vendor"
         / "MANIFEST.json").read_text())


def verify_vendor_files(vendor_dir: str | Path | None = None) -> list[str]:
    """Verify vendored sources + license against the trusted MANIFEST.

    Run BEFORE copying/building anything: a changed vendor file fails
    here, not downstream. Returns problems (empty = provenance intact).
    """
    vendor = Path(vendor_dir) if vendor_dir is not None else _vendor_dir()
    problems: list[str] = []
    for name, want in _manifest()["files"].items():
        p = vendor / name
        if not p.is_file() or p.is_symlink():
            problems.append(f"vendor {name} missing")
        elif _sha256_bytes(p.read_bytes()) != want["sha256"]:
            problems.append(f"vendor {name} differs from trusted manifest")
    return problems


def build_context(dest: str | Path, vendor_dir: str | Path | None = None,
                  ) -> dict:
    """Write a reproducible build context; return file manifest with hashes.

    Layout mirrors the upstream repo paths the Dockerfile COPYs:
      <dest>/Dockerfile
      <dest>/agent-langgraph-agui/requirements.txt
      <dest>/agent-langgraph-agui/src/*.py   (vendored, hash-verified)
      <dest>/shared/model_providers.py + model-providers.json
      <dest>/constraints-me.txt               (resolved pins, B verified)
    """
    vendor_problems = verify_vendor_files(vendor_dir)
    if vendor_problems:
        raise ValueError("vendor provenance failed: "
                         + "; ".join(vendor_problems))
    dest = Path(dest)
    if dest.is_symlink():
        raise ValueError(f"refusing symlink stage destination: {dest}")
    src_dir = dest / AGENT_DIR / "src"
    shared_dir = dest / "shared"
    src_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}

    def _write(path: Path, data: bytes) -> None:
        path.write_bytes(data)
        manifest[str(path.relative_to(dest))] = _sha256_bytes(data)

    # Generated Dockerfile = base recipe + BUILD-001 (constraints
    # actually consumed). The base stays byte-identical upstream.
    _write(dest / "Dockerfile", patched_dockerfile().encode())
    _write(dest / AGENT_DIR / "requirements.txt",
           UPSTREAM_REQUIREMENTS.encode())
    _write(dest / "constraints-me.txt",
           ("\n".join(RESOLVED_PINS) + "\n").encode())
    vendor = Path(vendor_dir) if vendor_dir is not None else _vendor_dir()
    for name in VENDOR_SRC_FILES:
        _write(src_dir / name, (vendor / name).read_bytes())
    for name in VENDOR_SHARED_FILES:
        _write(shared_dir / name, (vendor / name).read_bytes())
    _write(dest / ".me-owned-stage",
           f"openbot-agent-langgraph-agui:{AGENT_PIN}\n".encode())
    return {"dest": str(dest), "files": manifest,
            "model_patches": list(MODEL_PATCHES)}


def verify_build_context(dest: str | Path) -> list[str]:
    """Verify a staged context against the trusted MANIFEST (not self).

    Every produced stage file (Dockerfile, requirements, constraints,
    vendored sources) is hashed and compared to the in-repo anchored
    expected hashes. Changing staged requirements OR vendored sources
    before build fails here.
    """
    dest = Path(dest)
    problems: list[str] = []
    trusted = _manifest()
    expected_stage: dict[str, str] = {
        "Dockerfile": trusted["dockerfile"]["patched_sha256"],
        f"{AGENT_DIR}/requirements.txt":
            trusted["generated"]["requirements_txt_sha256"],
        "constraints-me.txt":
            trusted["generated"]["constraints_txt_sha256"],
    }
    for rel, want in expected_stage.items():
        p = dest / rel
        if not p.is_file() or p.is_symlink():
            problems.append(f"missing stage file {rel}")
        elif _sha256_bytes(p.read_bytes()) != want:
            problems.append(f"stage file {rel} differs from trusted recipe")
    for name, want in trusted["files"].items():
        if name in ("LICENSE.openbot",):
            continue  # provenance record, not shipped into the image
        if name in ("model_providers.py", "model-providers.json"):
            rel = f"shared/{name}"
        else:
            rel = f"{AGENT_DIR}/src/{name}"
        p = dest / rel
        if not p.is_file() or p.is_symlink():
            problems.append(f"missing stage file {rel}")
        elif _sha256_bytes(p.read_bytes()) != want["sha256"]:
            problems.append(f"stage file {rel} differs from trusted pin")
    marker = dest / ".me-owned-stage"
    if not marker.is_file():
        problems.append("missing .me-owned-stage ownership marker")
    return problems


def resolve_bot_settings(env: dict | None = None) -> dict:
    """Resolve (provider, model, key_variable, base_url_variable) via the REAL loader.

    Uses the vendored `model_providers.bot_settings("agent-langgraph-agui")`
    with BOT_SPEC_PATH pointed at the vendored spec, so the resolution order
    (env > spec file > provider default) is upstream's, not a reimplementation.
    Raises RuntimeError on missing row/spec (same as upstream).
    """
    import importlib
    import sys

    vendor = _vendor_dir()
    old_spec = os.environ.get("BOT_SPEC_PATH")
    os.environ["BOT_SPEC_PATH"] = str(vendor / "model-providers.json")
    sys.path.insert(0, str(vendor))
    try:
        import model_providers as mp  # noqa: PLC0415 (vendored loader)
        importlib.reload(mp)  # re-read spec from BOT_SPEC_PATH
        mapping = dict(env) if env is not None else dict(os.environ)
        settings = mp.bot_settings("agent-langgraph-agui", env=mapping)
        row = mp.SPEC["providers"]
        provider = settings.provider
        if provider not in row:
            return {"provider": provider, "model": settings.model,
                    "supported": False,
                    "reason": f"provider {provider!r} not in upstream spec "
                              "(openai|anthropic|google)"}
        return {"provider": provider, "model": settings.model,
                "key_variable": row[provider]["key_variable"],
                "base_url_variable": row[provider]["base_url_variable"],
                "supported": True}
    finally:
        try:
            sys.path.remove(str(vendor))
        except ValueError:
            pass
        sys.modules.pop("model_providers", None)
        if old_spec is None:
            os.environ.pop("BOT_SPEC_PATH", None)
        else:
            os.environ["BOT_SPEC_PATH"] = old_spec


def ollama_route(ollama_base: str | None = None,
                 model: str | None = None,
                 in_container: bool = False) -> dict:
    """Local-first Ollama via OpenAI-compatible endpoint (verified pattern).

    Upstream's spec knows only openai/anthropic/google; `BOT_PROVIDER=ollama`
    is NOT a supported config (rejected by `config_check`). The supported
    local route keeps `BOT_PROVIDER=openai` and points `OPENAI_BASE_URL`
    at Ollama's OpenAI-compatible API. Verified below by constructing the
    real model object with no network.

    Container reachability: inside the image, `127.0.0.1` is the
    container itself, NOT the host. Pass `in_container=True` (or an
    explicit `ollama_base`) for image runs: loopback defaults are
    rewritten to `http://host.docker.internal:11434` (Docker Desktop;
    Linux uses the host gateway / `--add-host` mapping, set explicitly).
    """
    base = (ollama_base or os.environ.get("ME_OLLAMA_URL")
            or "http://127.0.0.1:11434").rstrip("/")
    if in_container and not (ollama_base or os.environ.get("ME_OLLAMA_URL")):
        base = "http://host.docker.internal:11434"
    return {"BOT_PROVIDER": "openai",
            "OPENAI_BASE_URL": base + "/v1",
            "BOT_MODEL": model or os.environ.get("ME_OLLAMA_MODEL")
            or "llama3.1"}


def verify_ollama_model_object(env: dict) -> dict:
    """Construct the real upstream model object for the Ollama route.

    Calls the vendored `main._model()` semantics via langchain's
    `init_chat_model` exactly as upstream does (provider=openai +
    OPENAI_BASE_URL). Construction is lazy (no network, no spend).
    Returns {ok, model_class, base_url} or {ok False, error}.
    """
    saved = {k: os.environ.get(k) for k in
             ("BOT_PROVIDER", "BOT_MODEL", "OPENAI_BASE_URL",
              "OPENAI_API_KEY", "CHATGPT_AUTH_FILE")}
    try:
        os.environ.pop("CHATGPT_AUTH_FILE", None)
        for k, v in env.items():
            os.environ[k] = v
        os.environ.setdefault("OPENAI_API_KEY", "config-check-only")
        from langchain.chat_models import init_chat_model
        settings = resolve_bot_settings(env)
        if not settings.get("supported"):
            return {"ok": False, "error": settings.get("reason", "")}
        # Same normalize upstream main._normalize_openai_base_url applies
        # (blank strips to unset); inlined to avoid importing the serving
        # module (which builds the graph + FastAPI app at import).
        _base = os.environ.get("OPENAI_BASE_URL")
        if _base is not None:
            _base = _base.strip()
            if _base:
                os.environ["OPENAI_BASE_URL"] = _base
            else:
                os.environ.pop("OPENAI_BASE_URL", None)
        model = init_chat_model(
            settings["model"], model_provider=settings["provider"])
        base_url = getattr(getattr(model, "client", None), "base_url", None)
        return {"ok": True, "model_class": type(model).__name__,
                "provider": settings["provider"], "model": settings["model"],
                "base_url": str(base_url or os.environ.get("OPENAI_BASE_URL"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def config_check(env: dict | None = None) -> dict:
    """Structural config validation. No network, no secrets in output.

    Returns {ok, provider, model, supported, key_present, tokens_present,
    problems[]}. `ok` means the container would boot and resolve a model
    (config-valid). It does NOT mean inference works (see readiness).
    Absent provider/model fall through to spec defaults per upstream;
    absent tokens/keys are reported, not accepted as ready.
    """
    src = dict(os.environ) if env is None else dict(env)
    problems: list[str] = []
    for name in REQUIRED_TOKENS:
        if not (src.get(name) or "").strip():
            problems.append(f"{name} is not set")
    provider_raw = (src.get("BOT_PROVIDER") or "").strip().lower()
    if provider_raw == "ollama":
        problems.append("BOT_PROVIDER=ollama is not supported by upstream "
                        "spec; use the Ollama OpenAI-compatible route "
                        "(BOT_PROVIDER=openai + OPENAI_BASE_URL=<ollama>/v1)")
    elif provider_raw and provider_raw not in ("", "openai", "anthropic",
                                                "google"):
        problems.append(f"BOT_PROVIDER={provider_raw!r} unknown to upstream "
                        "spec (openai|anthropic|google)")
    try:
        resolved = resolve_bot_settings(src)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "problems": problems + [f"spec: {exc}"[:200]]}
    if not resolved.get("supported"):
        problems.append(resolved.get("reason", "unsupported provider"))
    else:
        key_var = resolved.get("key_variable", "")
        base_var = resolved.get("base_url_variable", "")
        # Key may be omitted only when a compatible base URL is explicitly
        # set (local Ollama / gateway). Otherwise the provider key is required.
        if not (src.get(key_var) or "").strip() and not (
                src.get(base_var) or "").strip():
            problems.append(f"{key_var} is not set and no {base_var} override")
    return {"ok": not problems, "provider": resolved.get("provider"),
            "model": resolved.get("model"),
            "supported": resolved.get("supported", False),
            "key_variable": resolved.get("key_variable"),
            "base_url_variable": resolved.get("base_url_variable"),
            "tokens_present": all((src.get(t) or "").strip()
                                  for t in REQUIRED_TOKENS),
            "problems": problems}


def readiness_check(env: dict | None = None) -> dict:
    """Config-valid AND live-gated readiness. Explicit live probe only.

    `ready` requires: config ok + model endpoint reachable (cheap,
    credential-free TCP/HTTP probe of the configured base URL; no chat
    call, no spend) + tokens present. Intelligence/license/entitlement
    stay with A's doctor (reported as delegated, never guessed here).
    """
    cfg = config_check(env)
    if not cfg.get("ok"):
        return {"ready": False, "config": cfg, "endpoint": None,
                "delegated": ["intelligence/license/entitlement (A doctor)"],
                "problems": cfg.get("problems", [])}
    src = dict(os.environ) if env is None else dict(env)
    base_var = cfg.get("base_url_variable") or ""
    endpoint = (src.get(base_var) or "").strip() or None
    if cfg.get("provider") == "openai" and not endpoint:
        endpoint = "https://api.openai.com"
    probe: dict = {"endpoint": endpoint, "reachable": None}
    problems: list[str] = []
    if endpoint and endpoint.startswith("http://127.0.0.1"):
        import socket
        from urllib.parse import urlparse
        try:
            host, port = urlparse(endpoint).hostname, urlparse(
                endpoint).port or 80
            with socket.create_connection((host, port), timeout=3):
                probe["reachable"] = True
        except Exception as exc:  # noqa: BLE001
            probe["reachable"] = False
            probe["error"] = f"{type(exc).__name__}"[:80]
            problems.append(f"model endpoint unreachable: {endpoint} "
                            "(NEEDS_SETUP: start Ollama/gateway)")
    elif endpoint:
        probe["reachable"] = "unknown"
        probe["note"] = "remote endpoint not probed (no spend without consent)"
    return {"ready": not problems and cfg.get("tokens_present", False),
            "config": cfg, "endpoint": probe,
            "delegated": ["intelligence/license/entitlement (A doctor)"],
            "problems": problems + ([] if cfg.get("tokens_present") else
                                    ["server tokens absent"])}


def agui_url(base: str) -> str:
    return base.rstrip("/") + AGENT_AGUI_PATH


def health_url(base: str) -> str:
    return base.rstrip("/") + AGENT_HEALTH_PATH


def docker_run_args(image: str, env_file: str, port: int = AGENT_PORT) -> list[str]:
    """Exact container launch for a built image (see build_context)."""
    return ["docker", "run", "--rm", "--name", "openbot-agent-langgraph-agui",
            "-p", f"127.0.0.1:{port}:4206",
            "--env-file", env_file, image]


def service_manifest(base_url: str = "http://127.0.0.1:4206") -> dict:
    return {"repo": AGENT_REPO, "ref": AGENT_PIN, "dir": AGENT_DIR,
            "port": AGENT_PORT, "agui": agui_url(base_url),
            "health": health_url(base_url),
            "token_header": TOKEN_HEADER,
            "entrypoint": ["uvicorn", "src.main:app",
                           "--host", "0.0.0.0", "--port", "4206"],
            "transport": "POST / is AG-UI (RunAgentInput); GET /health anonymous; "
                         "all other paths require x-openbot-agent-token == "
                         "MANAGED_AGENT_TOKEN",
            # OPENBOT_TOOL_URL is the FULL callback endpoint (default
            # http://127.0.0.1:3001/api/agent-tools/call); the agent POSTs
            # it as-is with NO path appended (verified in vendored
            # tool_runtime._call_tool). Never double-append the path.
            "tool_callback": "POST {OPENBOT_TOOL_URL} "
                             "(full endpoint, no path appended)",
            "model_patches": list(MODEL_PATCHES)}


def tool_callback_url(base: str) -> str:
    """Return the FULL tool-callback endpoint for a deployment base URL.

    Upstream POSTs `OPENBOT_TOOL_URL` verbatim; this helper is the one
    place the `/api/agent-tools/call` suffix is attached, so callers can
    never double-append it (already-full input is returned unchanged).
    """
    stripped = base.rstrip("/")
    if stripped.endswith("/api/agent-tools/call"):
        return stripped
    return stripped + "/api/agent-tools/call"
