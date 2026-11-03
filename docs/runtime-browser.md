# Isolated interactive browser (R7)

Status: implemented. `browser_open` is a real interactive browser that runs
ONLY in a dedicated Docker container. There is no host-Chrome fallback in
production.

## Architecture

- Engine: maintained Playwright (Python SDK `1.64.1`) + official container
  image `mcr.microsoft.com/playwright/python:v1.64.1-noble` (Ubuntu 24.04
  LTS Noble; see `docker/browser/Dockerfile`; build is operator-only,
  runtime uses `--pull never`). Version verified against the primary docs
  (https://playwright.dev/python/docs/docker) and the MCR tag list, which
  publish `:v1.64.1-noble` as the current Noble release; the SDK pin
  matches the image tag so the container browser engine and host tooling
  agree.
- Container flags (see `build_browser_docker_cmd`): `docker run -i --rm
  --pull never --network none --user 65532:65532 --cap-drop ALL
  --security-opt no-new-privileges --cpus 1.0 --memory 512m --pids-limit 128
  --read-only --tmpfs /tmp:rw,size=256m -e HOME=/tmp`, plus a single
  read-only mount `<tools>:/worker:ro`. No home filesystem, credentials,
  sockets, or CDP ports are mounted/exposed. The browser has no NIC, so
  page JS/WebRTC cannot reach home/LAN.
- Protocol: bounded JSONL stdio (`browser-worker-v1`). Host sends
  `open`/`snapshot`/`act`/`screenshot`/`close`; the worker intercepts every
  HTTP(S) request via a Playwright route and emits `fetch` request events.
  The host fulfills each via the vetted broker (`broker_fetch_raw`) and
  replies with `fetch_response` (base64-bounded payload). The host reads
  through a bounded binary `JsonlFramer` (partial frames buffered,
  batched frames drained in order, oversize failed closed, per-command
  deadline + cancel kill the container); `stdout` never grows unbounded.
  Frames are capped at 2MiB; actions per command at 8; URLs/selectors/text
  at bounded sizes. Service workers / websockets / downloads / non-http(s)
  are blocked; no arbitrary eval tool is exposed and no CDP port exists.
- Broker: allowlisted host, per-hop DNS pinning (every address must be
  global), manual redirects (max 3, each re-validated), bounded
  headers/body/timeouts, `-q` first + `--noproxy *` + sanitized env, `--`
  terminator, plus `--data-binary @-` with the bounded body on stdin for
  state-changing methods (multipart boundaries/content-types ride in the
  `-H` headers). `Authorization`/`Cookie` are stripped on cross-origin
  redirects. `GET`/`HEAD` flow by default; any other method requires
  `allow_write=True`, which the host sets only inside an approved-action
  window (page background POSTs are blocked otherwise). Failures fail
  closed; cookies/auth/bodies are never logged.

## Tool + approval gate

`browser_open(url="", session_id="", actions=None)` extends the existing
tool (9 tool names unchanged). One `BrowserManager` is shared per bridge
run (tool closure), so iterative `open` → `snapshot` → `act` calls reuse
the same sessions/containers; session storage persists under
`.me_runtime/browser/` so a resume in a fresh process reopens the same
scoped page.

- Readonly `open` (GET navigation), `snapshot`, `scroll`, `screenshot`
  need no approval and cause no write effects.
- `click` / `fill` / `press` pause via LangGraph `interrupt()` with
  `kind: awaiting_approval`, `action: browser_act`, exact
  `args: {session_id, url, actions, page_fingerprint, target_descriptor,
  target_fingerprint}` plus `args_hash` (existing convention) and
  `run_id`. Session ids are allocated once per LangGraph tool call
  (`InjectedToolCallId`-keyed, persisted in
  `.me_runtime/browser/_alloc.json`), so parallel calls get distinct
  sessions while interrupt replay/resume reproduces the identical
  session/action `args_hash`. The home expires/namespaces approvals; the
  bridge verifies binding, rejects changed `args`, stale fingerprints
  (page re-opened on resume and compared, including the real target
  descriptor — same visible text with a changed href/form destination or
  a remapped ref rejects), replays (pending is consumed once; `run_id`
  reuse is rejected), and capability/allowlist expansion (approvals can
  never widen egress). Only the approved actions run, and only inside the
  bounded approved window; unrelated background writes stay denied.
- State is snapshotted before interrupt; resume reopens the scoped page
  safely (cookies from `.me_runtime/browser/<session>/`, never artifacts)
  and executes only after `approve`, never on `reject`/stale. Scope is
  strictly run/user/conversation; zero owned orphans remain on
  cancel/exit/wait (bridge kills + removes containers and closes pipes;
  file reads for session state are bounded).

Returns real URL/title/body (first 8000 chars) plus up to 50 actionable
elements (`eN [role] name -> href/form destination`) for iterative model
decisions, and a bounded PNG artifact
(`output/browser/screenshot-<session>.png`) when captured.

## What is NOT claimed

- Interactive writes outside an approved action window are explicitly
  denied (`write blocked without approved action`), not faked.
- Without Docker + the pre-provisioned image, the tool fails closed with
  `prerequisite_missing`. There is no environment variable, API field, or
  model request that enables a host browser in production. Synthetic-DOM
  smoke tests inject a worker through an explicit test-only
  `popen_factory` (`test_worker_popen_factory`); production construction
  never sets one.
