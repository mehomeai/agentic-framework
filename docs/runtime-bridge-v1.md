# runtime-bridge-v1 — agent runtime CLI bridge contract

Version: `runtime-bridge-v1` · Status: implemented (R6) · Owner: `agentic-framework`

This is the small, versioned bridge the Node home service (`infra-server`)
calls to execute one agent run. Task state/identity stays owned by the home
service; the runtime is a stateless-per-invocation subprocess worker whose
agent loop is owned by the maintained LangGraph prebuilt ReAct harness
(`langgraph.prebuilt.create_react_agent`, pinned `langgraph==1.2.19` /
`prebuilt==1.1.1`; upstream deprecated the name since 1.0 in favour of
`langchain.agents.create_agent` to be removed in V2 — the alias remains
supported in 1.x and preserves 0.6 checkpoint resume, so it is kept here
with explicit `ToolNode(handle_tool_errors=True)` restoring 0.6 denial-as-
ToolMessage semantics) with a persisted SqliteSaver
checkpoint. There is no custom planner, no manual `/chat/completions`
parsing, and no parallel task scheduler in Me code.

Request/event shapes below are unchanged from R1 (H2-compatible). R2
clarified enforcement semantics (§4, §5, §6) and checkpoint files (§3);
R3 hardened transport/sandbox/browser (§4) and verified the live Responses
path (§5); R4 closed curl-config/proxy bypasses, bounded streaming, and
removed writable host mounts (§4); R5 corrects the output model (§4):
tmpfs dies with the container, so streams — not `docker cp` — are the
artifacts; R6 closes the reserved-state file boundary and bounds file
reads/listings/writes (§3, §4). Behavior corrections are marked
**[R2]**–**[R6]**.

## Invocation

```sh
# from the agentic-framework checkout (installed venv)
/Users/kgensupertramp/Desktop/me/agentic-framework/.venv/bin/python -m me_runtime.bridge < request.json > events.jsonl 2> diagnostics.log
echo "exit=$?"
```

- **stdin:** exactly one JSON object (the run request, §1).
- **stdout:** JSONL — one JSON object per line (§2). `tool_call` /
  `tool_result` pairs are replayed from checkpointed harness messages after
  the run; `artifact` events follow tool execution.
- **stderr:** human-readable diagnostics/logs only, never secrets.
- **Exit codes:** `0` terminal result; `2` invalid request/usage;
  `130` cancelled by signal/deadline.

## 1. Request (stdin)

Shape unchanged from R1:

```json
{
  "bridge": "runtime-bridge-v1",
  "op": "run",
  "run_id": "run_01J...",
  "user_id": "alice",
  "conversation_id": "conv_abc",
  "workspace": "/home/data/tasks/run_01J...",
  "request": "Summarise the attached notes.",
  "inputs": [],
  "approvals": [],
  "capabilities": {
    "files": true,
    "web_fetch": false,
    "browser": false,
    "code_exec": false,
    "animation": false,
    "network_egress_allowlist": []
  },
  "model": { "provider": "local", "model": "llama3.1" },
  "budgets": { "max_steps": 8, "max_wall_seconds": 120, "max_tool_calls": 20 },
  "context_refs": []
}
```

Field rules: `run_id`/`user_id`/`conversation_id`/`workspace`/`capabilities`/
`model` required as in R1. `op: run` executes; `op: resume` continues a run
that ended `waiting_input`/`awaiting_approval`, with the same
`run_id`/`user_id`/`conversation_id`/`workspace`. Re-invoking `op: run`
with a `run_id` already used in that workspace is rejected as
`invalid_request` (no blind replay; the home mints a new run id).

**[R2]** `model` additions: `endpoint` (explicit base URL for
`openai`/`anthropic`; default local endpoint for `local`) and
`use_responses_api` (boolean, `openai` only, best-effort — ignored with a
stderr note if the installed SDK does not accept it).

**[Context]** `context_refs` consumes the existing home contract (bounded
same-conversation window, owner-scoped by the home): at most 10
`{kind: "message", role: "user"|"assistant", text}` turns, each ≤2000
chars, ≤20000 chars aggregate; unknown kinds/roles ignored, non-list
rejected as `invalid_request`. Consumed on `op: run` only as
`HumanMessage`/`AIMessage` turns before the current request (untrusted
data, never system/developer/tool); a trailing turn byte-identical to the
current request is deduped once (usual phone post-then-task). `op: resume`
never reinjects (persisted graph/checkpoint only). History lives only in
the private `graph.db`, never in artifacts/logs. Final extraction uses
latest-AI authoritative semantics: an empty latest AI never reuses prior
assistant text. This is bounded conversation context, not semantic memory
(no retrieval/scheduler/embeddings).

## 2. Events (stdout JSONL)

Unchanged shapes. Terminal `result.status`: `completed` | `failed` |
`cancelled` | `waiting_input` | `awaiting_approval`. The `started` event
additionally carries `harness: "langgraph-prebuilt-react"` and the resolved
`model` provider/name. Error codes: `invalid_request`, `workspace_denied`,
`capability_denied`, `approval_rejected`, `budget_exhausted`, `cancelled`,
`model_error`, `model_not_configured`, `tool_error`, `prerequisite_missing`,
`internal`.

## 3. Workspace layout

```
<workspace>/
  input/            # home-placed source files
  output/           # runtime-written deliverables (declared as artifacts)
  .me_runtime/
    graph.db        # persisted LangGraph SqliteSaver checkpoint
    pending.json    # [R2] pending interrupt payload + trusted binding
                    # (run_id/user_id/conversation_id/workspace,
                    #  exact action/args/args_hash)
```

The runtime creates `output/` and `.me_runtime/`. All file reads/writes
resolve inside `workspace` (symlink escapes denied). Delete/ignore any
R1-era `.me_runtime/checkpoint.json` / `events.jsonl` — they are superseded.

**[R6]** `.me_runtime/` (`graph.db` + trusted `pending.json`) is a
reserved internal namespace: `files_read` / `files_list` / `files_write`
deny it AFTER symlink resolution, so normalized `..`/`.` paths and
same-workspace symlink aliases into it fail closed as `workspace_denied`;
`files_list` on a parent hides reserved entries instead of leaking them.
The runtime itself still reaches state via the internal `Workspace`
jail (`resolve()` / `ensure_dirs()` / `root`), which is intentionally
distinct from the public `resolve_public()` / bounded `read_text` /
`list_dir` / `write_bytes` used by model tools.

**[R9]** `graph.db` uses strict allowlist-only checkpoint deserialization
(`LANGGRAPH_STRICT_MSGPACK=true` at CLI bootstrap plus explicit
`JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)`);
no pickle fallback or unrestricted constructors. Unsafe payloads fail closed;
legacy 0.6 trusted checkpoints resume via the same harness.

## 4. Capabilities (default deny) — [R2] enforcement

Exactly nine brokered tools exist; the agent receives no framework default
filesystem/shell/subagent tools (`EXPECTED_TOOL_NAMES` is asserted at build):

`files_read`, `files_list`, `files_write` (need `files`);
`web_fetch` (needs `web_fetch`);
`browser_open` (needs `browser` **and** `web_fetch`);
`run_code` (needs `code_exec`);
`animation_build` (needs `animation`);
`ask_user`, `request_approval` (always available; they only pause for home input).

- `files_*` — capability checked on every call; read results return to the
  harness so the model reasons over real content across turns.
  **[R6]** `files_read` streams at most 1MiB from regular files only
  (FIFOs/devices/sockets/dirs rejected via `fstat`, chunked reads — never
  `read_bytes()`-then-truncate, so giant files cannot OOM the worker);
  `files_list` iterates bounded (max 200, no `list(iterdir)[:limit]`
  materialization) and hides `.me_runtime` entries/aliases; `files_write`
  enforces a 10MiB per-file cap and a 20MiB per-run aggregate quota and
  fails closed instead of silently truncating (artifacts stay
  home-compatible). Capability flags are strict booleans at entry
  (`"true"`/`1` rejected as `invalid_request`; home remains the owner of
  enforcement). Protocol and tool names are unchanged.
- `web_fetch` — **[R3]** curl-backed broker (maintained TLS; no custom
  crypto). Every hop is validated: http/https only, no URL credentials,
  default ports only (80/443), allowlist match (exact or leading-dot
  suffix), and DNS resolution where **every** returned address must be
  globally routable (private/loopback/link-local/multicast/reserved/
  unspecified all fail closed). The vetted IP is pinned per hop
  (`curl --resolve host:port:ip`) while SNI/TLS verification still uses
  the hostname, closing the resolve-to-connect TOCTOU gap. Redirects are
  followed manually (max 3, each target re-validated); bodies capped at
  500_000 bytes (`--max-filesize`), timeouts bounded (5s connect/15s
  total). **[R4]** curl runs with `-q` as the first flag (user curlrc
  ignored), `--noproxy '*'` plus a proxy-stripped environment (proxies
  must not bypass pinning), a `--` terminator before the URL, and
  bracketed IPv6 in `--resolve`. Stdout/stderr stream with hard caps
  (256KB headers, 1MB total) while the process runs — independent of
  server chunking/content-length — and oversized responses are killed,
  not buffered. **[R2]** a non-allowlisted host fails immediately with
  `capability_denied` — an approval record can never expand the allowlist.
- `browser_open` — **[R7]** real **isolated interactive** browser
  (supersedes the R3 offline renderer). `browser_open(url="",
  session_id="", actions=None)` runs Playwright ONLY in a dedicated
  `--network none` container (unpriv uid, cap-drop, no-new-privs,
  read-only root, tmpfs caps, readonly `/worker:ro` mount only; no host
  Chrome fallback in production). Every request is intercepted and
  fulfilled via the vetted broker (`broker_fetch_raw`: binary/method/
  headers/body, credential-strip on cross-origin redirect, `GET`/`HEAD`
  by default, writes only inside an approved-action window). Returns real
  URL/title/body plus up to 50 actionable elements and a bounded PNG
  artifact (`output/browser/screenshot-<session>.png`). Readonly
  open/scroll/screenshot need no approval; `click`/`fill`/`press` pause
  with `awaiting_approval` (`action: browser_act`, exact URL/action/
  target/value + page/target fingerprint over the real target descriptor
  (role/name/href/formAction/formMethod/inputType/tag),
  `args_hash`/`run_id` binding; stale/replay/changed-state/changed-
  destination rejected, egress never expanded). One `BrowserManager` is
  shared per run with `InjectedToolCallId`-keyed persisted session
  allocation, so replays reproduce identical ids; host reads use a bounded
  binary framer (deadline + cancel kill). Request bodies travel via
  `--data-binary @-` on stdin (bounded, never logged). Session
  cookies persist only in `.me_runtime/browser/`; zero owned orphans on
  cancel/exit/wait. Missing Docker/image fails closed with
  `prerequisite_missing` (no env/API/model switch enables a host
  browser). Full contract in [runtime-browser.md](runtime-browser.md);
  provision in `docker/browser/BUILD.md`.
- `run_code` — Docker one-shot container only (`ME_RUNTIME_SANDBOX=docker`,
  `--network none`, uid `65532:65532`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--workdir /work`, CPU/memory/pid
  limits, pre-provisioned image, no implicit pulls). **[R4]** there are no
  writable host mounts at all: entry script and workspace input mount
  read-only (`/src`, `/input`); all container writes go to a 64MB tmpfs
  `/work` (plus 64MB noexec `/tmp`), so generated code cannot fill home
  disk. **[R5]** container files are ephemeral by lifecycle (tmpfs is
  destroyed when the container stops), so no `docker cp` collection is
  attempted — the bounded stdout/stderr streams are saved as
  `output/exec/stdout.txt` / `stderr.txt` and those are the artifacts.
  Code needing durable files should print (or encode) them to stdout.
  **[R3]** stdout/stderr stream with hard caps (1MB/256KB) while the
  process runs; timeout (`ME_RUNTIME_CODE_TIMEOUT_S`, default 60s) or
  cancellation kills **and** removes the owned container. **[R2]** there
  is no host fallback under any flag.
- `animation_build` — **[R2]** Pillow (maintained imaging engine) renders a
  real animated GIF plus editable `scene.py` (stdlib+Pillow; title/caption
  are data), `scene.json`, and a `preview.html` that references the GIF
  relatively. Preview files are download artifacts and must never be served
  inline on the authenticated home origin. MP4 export additionally needs
  `ffmpeg`; without it the GIF stands and the gap is reported, never faked.
- Approvals authorize a specific proposed action instance; they never grant
  a denied capability. A rejected or payload-changed decision ends the run
  as `failed` (`approval_rejected` / `invalid_request`) without executing.

## 5. Model adapters — [R2]

| Provider | Adapter (maintained SDK) | Rule |
| --- | --- | --- |
| `local` | `langchain-ollama` ChatOllama (default `http://127.0.0.1:11434`) | Local-first. Unreachable endpoint → honest `model_not_configured` failure. **[R9]** loopback ignores ambient proxy (`sync/async_client_kwargs trust_env=False`), so `HTTP(S)_PROXY` never routes local prompts off-host. |
| `openai` | `langchain-openai` ChatOpenAI, explicit `endpoint` base URL | Requires `ME_RUNTIME_ALLOW_REMOTE=1` **and** the key env var — even for localhost URLs, so a remote URL can never be mislabelled local to skip privacy checks. `use_responses_api: true` selects the Responses API. **[R3]** verified live: `muse-spark-1.3` via an OpenAI-compatible Responses endpoint drove a synthetic read-file → write-file tool loop to `completed` (no personal data; credential held in process env only, never logged/written). |
| `anthropic` | `langchain-anthropic` ChatAnthropic | Same opt-in rule as `openai`. |
| `fixture` | In-repo `FixtureChatModel` on the LangChain chat-model interface | **Tests only** (offline, deterministic, standard tool-call round trips). Directive prefixes (`ASK:`, `APPROVE:`, `READWRITE:`, …) are interpreted solely by this stub and are never a production planner. |

Keys come only from environment variables named by `api_key_env`, never
from the request; they are never logged or echoed into events.

## 6. Cancellation, failure, recovery — [R2]

- `waiting_input`/`awaiting_approval` pause via harness `interrupt()`;
  the payload (exact action/args/`args_hash` plus trusted
  run/user/conversation/workspace binding) persists in `pending.json`.
- `resume` re-opens the same SqliteSaver checkpoint (`thread_id=run_id`)
  and continues with `Command(resume=…)`; completed tool calls are already
  checkpointed and never replayed. Binding mismatches (wrong
  run/user/conversation/workspace, unknown id, changed `args`, missing
  answer/decision) fail closed as `invalid_request` without touching the
  graph; `reject` ends the run as `approval_rejected`.
- **[R9]** wall budget counts cumulative ACTIVE execution (`active_wall_seconds`
  in `pending.json`, monotonic per-process), not paused calendar wait.
  Human/child waits are excluded; model/tool continuations count. Resume with
  `priorActive >= max_wall_seconds` fails `budget_exhausted` before effects.
  Legacy `wall_start`-only checkpoints migrate one-time (`priorActive=0`).
  `max_steps` maps to a harness `recursion_limit` (shrunk on every resume, no
  reset loophole) → `budget_exhausted`.
- SIGTERM/SIGINT/deadline → owned-container kill **and remove**
  (best effort), terminal `cancelled`, exit `130`. Partial artifacts
  preserved.
- `max_steps` maps to a harness `recursion_limit` → `budget_exhausted`.
  Unrecoverable tool errors surface to the model and end the run as
  `RUN-FAILED <code>` → terminal `failed` with the tool's code.

## 7. Node home-service integration (handoff)

Unchanged from R1 — spawn `.venv/bin/python -m me_runtime.bridge`, write
one JSON request to stdin, parse stdout JSONL per `event`/`result.status`,
`child.kill("SIGTERM")` to cancel. Persist every event against `run_id`;
artifacts are workspace-relative (`workspace/<path>`, verify `sha256`).
Host env for optional backends:

```sh
export ME_RUNTIME_ALLOW_REMOTE=1            # synthetic remote-model tests only
export ME_RUNTIME_MODEL_API_KEY="..."       # never in request JSON
export ME_RUNTIME_SANDBOX=docker            # enable container code execution
export ME_RUNTIME_DOCKER_IMAGE_PY="python:3.12-slim"   # pre-provisioned
export ME_RUNTIME_DOCKER_IMAGE_NODE="node:24-slim"     # pre-provisioned
```

## 8. What R3 does NOT claim

- No hosted endpoints, cloud resources, phone/backend code.
- `browser_open` is offline rendering only (JS disabled, subrequests
  aborted); full interactive automation needs an isolated/proxied sandbox
  and fails closed. `run_code` needs Docker + pre-provisioned images;
  live models need real endpoints/keys. Absent backends produce explicit
  prerequisite/capability failures.
- Fixture-model tests are deterministic and offline; mocked
  transport/sandbox tests prove guards without network or Docker; the one
  live synthetic Responses check is opt-in, uses no personal data, and its
  credential never touches files/logs/source.
