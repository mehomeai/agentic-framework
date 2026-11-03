# runtime-delegation — R8 durable non-blocking delegation (runtime half)

Version: `runtime-bridge-v1` + `task-rpc-v1` · Status: implemented (R8) ·
Owner: `agentic-framework` · Home contract frozen:
`../infra-server/docs/runtime-task-rpc-v1.md` + `home-h6-handoff.md` (read-only).

The agent loop stays owned by the maintained LangGraph prebuilt ReAct
harness with persisted SqliteSaver checkpoint. No custom AI planner, no
second job engine. Home is the single durable authority (leases, limits,
fanout/depth/root, wake CAS, cancel cascade); the runtime only requests
narrowing and surfaces denials honestly.

## 1. task-rpc-v1 client (`src/me_runtime/task_rpc.py`)

- fd from `ME_HOME_TASK_RPC_FD` (default 3), duplex JSONL, possession is the
  capability. Main stdin stays exactly one JSON object read to EOF.
- Validates fd is socket/FIFO (not regular file/dir/device); dups + sets
  non-inheritable so Docker/curl/browser children never inherit it.
  `sanitize_child_env()` strips `ME_HOME_TASK_RPC_FD` + `*_PROXY` from every
  spawned transport (exec/curl/browser).
- Bounds (both sides): 64KiB line, 32KiB params, 128KiB response, 512KiB
  buffered without newline, 256KiB pending writes, 200 requests/run (201st
  local `delegation_denied`), IDs 1..128 chars or safe ints, methods 1..64,
  params objects only. Split UTF-8 via incremental decoder, batched lines in
  order, no unbounded `readline`. Thread-safe: single write lock + reader
  thread routing by ID for concurrent ToolNode calls.
- Cancellation/deadline honoured; missing RPC → honest `prerequisite_missing`
  (legacy runs unaffected); home errors (`bad_request/capability_denied/
  delegation_denied/delegation_conflict/not_found/wrong_state`) propagate
  with code + short message only — never request contents/credentials/logs.
  Unknown/cross-scope child IDs are denials, no fallback local tasks.

## 2. Brokered tools (`src/me_runtime/tools/delegate.py`)

Explicit allowlist only (asserted in `agent.py`):
`delegate_task/list_children/get_child/await_children` plus the nine R7 tools.

- `delegate_task(key, request, title?, capabilities?, budgets?)` — submits one
  independent child via `submit_child`. Key is the stable idempotency key
  (1..128) scoped to parent; same key + identical hash returns existing child,
  different hash is `delegation_conflict`. Home inherits owner/conversation/
  model and narrows caps/budgets; the tool can only request narrowing.
  Immediate return lets the parent continue other work then `ask_user`; no
  LLM polling loop. Denials (disabled/fanout/depth/root) surface honestly,
  no recursive bomb.
- `list_children()` / `get_child(id)` — direct children only, never global
  lookup. Unknown IDs are `not_found`.
- `await_children(child_ids?)` — lists direct children; when any target is
  active (`queued/running/waiting_*/`) interrupts with
  `{kind:waiting_children, children:[...], mode:interrupt}`; resumes with
  trusted home `child_results` when all terminal. All-terminal returns
  immediately with summaries (no interrupt). Results are bounded trusted DATA
  (success output ≤2000 chars + artifact metadata; failures/cancels included
  honestly), never treated as instructions.

Home selects owner/conversation/model/caps/budgets; fanout 3/depth 1/root 4
defaults (configurable, any 0 disables) are home-enforced.

## 3. Bridge (`src/me_runtime/bridge.py`, runtime-bridge-v1 compatible)

- New terminal `waiting_children` with `children:[ids]` (plus `child_ids`
  compat). Validates pending run/user/conv/workspace + expected child IDs;
  persistence via SqliteSaver `graph.db` (strict serde, [R9]) + private `pending.json` metadata
  (`kind/mode/children/seen/budgets/active_wall_seconds`). For Graph interrupts,
  resumes with `Command(resume={child_results})` — completed tool calls are
  never replayed. `waiting_input` parents may also have children; child
  settle never auto-resumes a human wait (only explicit `inputs` resumes;
  children fetched via RPC after resume).
- Automatic completion policy: a harness final with active children never
  publishes. The bridge stores private handoff (`mode:completed_early`,
  `pending_output`) and emits `waiting_children`; on wake it appends trusted
  bounded child DATA into the SAME checkpoint and invokes a continuation
  (same `thread_id`), not a rerun of the original goal/tools. Unseen
  terminal outputs are collected once (tracked `seen` IDs, checkpoint-
  idempotent); continuations honour cumulative `max_steps/recursion`,
  `max_tool_calls`, and active wall budgets (`active_wall_seconds` carried in pending, no
  silent reset; paused waits excluded, legacy `wall_start` migrates one-time). Child outputs are DATA, errors included honestly.
- Root `modeladapter` remote policy unchanged (`ME_RUNTIME_ALLOW_REMOTE`).

## 4. Fixture model (TEST-ONLY) + E2E

`FixtureChatModel` standard `tool_calls` for `DELEGATE:<key>:<req>`,
`DELEGATE_DUP:<key>:<req>`, `DELEGATE_WIDE:<key>:<req>`, `DELEGATE_WAIT`,
`COMPLETE_EARLY`, `WRITE_CHILD:`, `SLEEP:` plus existing `ASK:/READWRITE:`.
Child runs reuse existing directives, so the real home+runtime path (real
RPC, real harness, real checkpoint) is exercised — no fake planner.

- `tests/test_task_rpc.py` — client bounds, ID routing, concurrency,
  UTF-8/partial/batched, budgets, missing-fd, sanitization, cancellation.
- `tests/test_bridge_delegation.py` — fake-pipe bridge parks/resumes,
  prerequisite-missing, completion-policy hold, resume validation.
- `tests/run_home_delegation.mjs` — owned harness importing ACTUAL sibling
  home API (`../../infra-server/src/server.mjs`, no duplication). Strict
  gated skip if sibling/venv missing (`ME_RUNTIME_REQUIRE_HOME=1` fails).
  Cases: delegate→waiting_input with child-first + restart + single message;
  CAS wake with multiple children; premature-completed hold; idempotency +
  child-failure honesty; cancel cascade without orphans + cross-user isolation;
  disabled denials. Runs against actual H6; home `test/*.test.mjs` untouched.

Resources: no browser/model downloads; Playwright SDK smoke skips honestly
(production browser runs in its Docker image). Localhost fixtures + tmpdirs
only; no secrets, no user files.
