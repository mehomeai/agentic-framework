# Upstream vendor ledger — OpenBot agent-langgraph-agui @ 6bfee78

Upstream: `CopilotKit/OpenBot@6bfee780c8e8d05141375ea55ebde33eefb3dbc0`
License: MIT (`LICENSE.openbot`, tracked in this directory; preserve notice).
Source: raw.githubusercontent.com reads at pin (sparse, no clone).

Machine-readable provenance: `MANIFEST.json` (full 64-hex SHA256 for every
file below + base/patched Dockerfile + generated requirements/constraints).
`verify_vendor_files()` checks this tree against it BEFORE any copy/build;
`verify_build_context()` checks every staged file against it (never
self-comparison).

## Files (verbatim, unedited)

| File | SHA256 | Bytes | Upstream path |
| --- | --- | --- | --- |
| `tool_runtime.py` | `64b6eec8…99cb69` | 8476 | `agent-langgraph-agui/src/tool_runtime.py` |
| `parallel_tools.py` | `2b12eef7…676914c` | 8021 | `agent-langgraph-agui/src/parallel_tools.py` |
| `main.py` | `56e3203d…5e98b99c` | 10042 | `agent-langgraph-agui/src/main.py` (the served service) |
| `model_providers.py` | `ae500f6c…23d3ab4420` | 8248 | `shared/model_providers.py` |
| `model-providers.json` | `1f41a218…45676abed8d` | 2205 | `shared/model-providers.json` |
| `chatgpt_store.py` | `8a696a44…3f56624a` | 1496 | `agent-langgraph-agui/src/chatgpt_store.py` (reference; plan-token path not in default) |
| `LICENSE.openbot` | `6d35a6bb…9d2ca95bad` | 1067 | `LICENSE` (MIT; provenance record, not shipped into the image) |

## Compat patches (logic-preserving, reviewable)

Production is the unmodified vendored service itself (`main.py` + this
directory, staged by `agent_service.build_context()` and served as the
`agent-langgraph-agui` container). No production graph is recompiled
here; the two patches below apply ONLY to the devmode component-test
adapter in `tests/support_upstream/upstream_engine.py`, never to the
shipped path:

- PATCH-D1 checkpointer: upstream `main.py:235` compiles with
  `MemorySaver()` (explicitly non-durable: "two stores remembering the
  same thread is how they come to disagree"). Devmode component tests
  compile the SAME `StateGraph(MessagesState)` topology
  (`answer`/`tools`/`next_step`) with `SqliteSaver(strict serde, no
  pickle)` so devmode restarts survive the process. No node rewritten.
  Production durability (durable threads/memory) is owned by home-owned
  self-hosted Intelligence, not by either checkpointer.
- PATCH-D2 model hook: upstream `answer` calls `_model()` (env
  `BOT_PROVIDER`/`BOT_MODEL` + `init_chat_model`). The devmode
  `answer` mirrors upstream logic (`model_messages`, Anthropic
  `_system_turns_first`, typed provider-auth → `authentication_failed`)
  but takes the injected maintained model (fixture / local ChatOllama).
  No stub model. Production serves upstream `_model()` unchanged.
- Import-path note: vendored filenames keep upstream relative imports
  (`tool_runtime.py` → `.parallel_tools`, `main.py` → `.tool_runtime`,
  `.chatgpt_store`) working with zero logic edits.
- `chatgpt_store.py` (plan-token provider) vendored for completeness;
  NOT wired into the default provider hook (requires user-mounted store
  file; out of scope for CORE).

## Build recipe ledger

- Base Dockerfile = verbatim upstream `agent-langgraph-agui/Dockerfile`
  @ pin (sha256 `7ca6a2ca…404534`; CMD already all-string exec form —
  the numeric-port transcription error lived only in our old record).
- BUILD-001 (build-only; graph, imports, entrypoint, CMD unchanged):
  COPY `constraints-me.txt` + `pip install … -r requirements.txt
  -c constraints-me.txt` so the lock is consumed. Patched sha256
   `c8aa8f04…460202c0fd`. `constraints-me.txt` pins every framework
  version verified in this repo's venv that the image closure installs;
  dev-only `langgraph-checkpoint-sqlite` is excluded (requirements.txt
  only, never demanded from the image). Image-only `uvicorn`,
  `python-multipart`, `langchain-google-genai` resolve at build and are
  captured by Linux CI (`pip freeze` must match each constrained row).

## What executes by default

Production serves the vendored `main.py` app unchanged (`FastAPI` +
`add_langgraph_fastapi_endpoint`, token middleware, `ToolAwareAgent` +
`ParallelToolAgent(LangGraphAgent)` + `bind_tools`/`model_messages`/
`next_step`/`execute_tools` over the HTTP tool-callback path) from the
staged image context. See `results/runtime-wire-contract.json`.

`tests/support_upstream/upstream_engine.py` is devmode-only test
support (component tests with a controlled model + loopback gateway);
it is not shipped and owns no production transport, schedule, or
durable state.
