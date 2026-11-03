# Upstream runtime adoption: agent-langgraph-agui (production)

Production path: the pinned OpenBot `agent-langgraph-agui` container serves
AG-UI; the OpenBot server owns `/api/agent-tools/call`; home-owned
self-hosted Intelligence persists threads. This repo ships NO custom
production graph, NO second scheduler, NO handwritten SSE parser. Prior
audit: `results/runtime-contract.json` + candidates tree (2026-11-03).

## 1. What runs in production

- Source: `CopilotKit/OpenBot@6bfee780c8e8d05141375ea55ebde33eefb3dbc0`,
  dir `agent-langgraph-agui`, shared spec `shared/model_providers.py` +
  `shared/model-providers.json`. Vendored verbatim under
  `src/me_runtime/upstream/vendor/` (ledger: `VENDOR.md` with SHA256 per
  file; `main.py` byte-matches the exact-SHA sparse checkout).
- Model patches on top of the pin: none (`model_patches: []`).
- Image: built from the staged context, tagged
  `openbot-agent-langgraph-agui:6bfee780c8e8`; digest recorded by whoever
  runs `docker build`. No immutable official image is published for this
  harness; the reproducible recipe IS the image source.
- Entrypoint: `uvicorn src.main:app --host 0.0.0.0 --port 4206`.
  `POST /` = AG-UI (`RunAgentInput` → SSE). `GET /health` anonymous →
  `{ok, harness: langgraph}`. All other paths require
  `x-openbot-agent-token == MANAGED_AGENT_TOKEN`.
- Tool execution: upstream owns the graph, `next_step` ownership
  (deployment set → `tools` node; surface-owned → `END`; unoffered →
  `RUN_ERROR`), and the HTTP tool callback (`POST {OPENBOT_TOOL_URL}`
  where the env value is the FULL endpoint, default
  `http://127.0.0.1:3001/api/agent-tools/call` — posted verbatim, never
  extended; header `x-openbot-agent-token == AGENT_TOOL_TOKEN`, body
  `{name, args, run}`). Missing token refuses before any HTTP request;
  wrong token gets 401/403 — both become model-visible `ToolMessage`s,
  never 500s.
- Threads/memory: upstream `MemorySaver` is explicitly ephemeral;
  durable threads/memory are owned by home self-hosted Intelligence
  (licensed, Task A), never by a second Me scheduler. Legacy
  `pending.json`/custom engine stays accessible but new full mode never
  silently uses it.

## 2. Reproducible packaging (owned here)

- `src/me_runtime/upstream/vendor/MANIFEST.json`: in-repo trusted
  provenance — full 64-hex hashes for 6 vendored sources + MIT license,
  base vs patched Dockerfile, generated requirements/constraints.
- `src/me_runtime/agent_service.py`: `verify_vendor_files()` checks the
  vendor tree against MANIFEST BEFORE copying (tamper fails the build);
  `build_context(dest)` stages the patched Dockerfile (base + BUILD-001:
  `COPY constraints-me.txt`, `pip install -r requirements.txt
  -c constraints-me.txt`, so the lock is consumed) + requirements +
  vendored sources + `constraints-me.txt` + `.me-owned-stage` marker;
  `verify_build_context(dest)` hashes every staged file against
  MANIFEST (never self-comparison — changed requirements/sources fail);
  `pinned_dockerfile()` returns the verbatim upstream recipe (CMD
  already all-string exec form); `tool_callback_url()` is the single
  suffix-attachment point (FULL endpoint, idempotent);
  `config_check(env)` (structural, no presence-only READY);
  `readiness_check(env)` (config + live-gated loopback probe,
  Intelligence delegated); `ollama_route(..., in_container)` +
  `verify_ollama_model_object()` (local Ollama via OpenAI-compatible
  base URL + REQUIRED dummy `OPENAI_API_KEY=ollama` — the real SDK
  refuses keyless construction even with a base URL; verifier never
  invents credentials; URL alone is not bootable. Image runs use a
  container-reachable endpoint, never host loopback by accident;
  `BOT_PROVIDER=ollama` rejected). Exact namespace in
  `results/runtime-local-model-contract.json`.
- `scripts/stage_agent_image_context.py <dest>`: stdlib-only stager;
  refuses symlink destinations and foreign nonempty dirs, safe owned
  re-run, no destructive cleanup; prints manifest + `docker build`/`run`.
- Linux acceptance: `.github/workflows/upstream-agent-image.yml`
  builds the image, probes `/health` + 401 middleware in the REAL
  container, asserts `pip freeze` honours every constrained row, and
  runs the full suite incl. google + docker cases. CI run
  [41731651037](https://github.com/mehomeai/agentic-framework/actions/runs/41731651037)
  green: image freeze langchain-core 1.7.4 / google-genai 4.4.2 /
  uvicorn 0.54.0 / multipart 0.0.33 / anthropic 2.4.0, provider freeze
  same core+google+anthropic with openai 3.42.1 — all now locked in
  `constraints-me.txt` (image had floated openai 3.44.0, now pinned
  3.42.1; next image CI revalidates health at that pin).
- Full wire details: `results/runtime-wire-contract.json`.

## 3. Proven by tests (no live spend, no Docker, no keys)

- `tests/test_agent_packaging.py` (8 tests): recipe regressions —
  base/patch hashes anchored, BUILD-001 minimal diff, CMD all-string
  exec form, constraints referenced + consumed, vendor-tamper fails
  before build, staged-drift fails verify, callback URL never
  double-appends, container Ollama reachability, stager
  symlink/foreign-dir refusal + safe re-run.
- `tests/test_upstream_entrypoint.py` (8 tests): REAL vendored `main.py`
  app over ASGI, env-isolated — health contract, token middleware,
  AG-UI POST through the real endpoint/graph/nodes with enforcing
  callback auth (correct-token success vs missing-token pre-request
  refusal vs wrong-token 401/403, no execution either way),
  unoffered-tool `RUN_ERROR`, run-secret privacy
  (assertion/tokens/forwarded props never reach model or events),
  same-thread resume across runs, Ollama-compatible route construction.
- `tests/test_upstream_suite.py` (2 tests): upstream's OWN vendored
  suite (`agent-langgraph-agui/tests`, verbatim) runs green against the
  staged tree — 78 passed; gating is conditional on the environment
  (Linux CI installs `langchain-google-genai` and has `docker`, so it
  runs everything; this Mac deselects 11, each proven to fail only for
  its stated environmental reason). This is the continuous pin-update
  tripwire: bump the pin, re-vendor, this suite surfaces upstream
  regressions first.
- `tests/test_upstream_agui.py` (2 tests, explicit DEVMODE): component
  coverage of the real vendored classes with fixture model + loopback
  gateway (deployment callback path, surface ask_user END + trusted
  resume on the same SQLite thread, checkpoint privacy). Useful, but NOT
  equivalent to the server tests above.
- Baseline suite: 122 pre-existing meaningful tests keep passing;
  `src/me_runtime/upstream/tests/` copies are not collected in place
  (they require the upstream repo layout) — they execute via the staged
  tree in `test_upstream_suite.py`.

## 4. What is NOT claimed

No live-model runs, no Intelligence E2E (license/creds are honest
`NEEDS_INPUT` operator gates via `config_check`/`readiness_check`), no
Docker/browser execution on this Mac, no phone/backend relay proof here.
Google-provider and ChatGPT-plan paths execute upstream code unchanged
but are provider-gated in this environment (see §3). No feature claims
beyond what the tests above prove.
