# agentic-framework

Agent orchestration framework for privacy-first home AI.

Part of [Me Home AI](https://github.com/mehomeai), a privacy-first home agentic system.
Learn about the vision at [Me — Your intelligence. Yours.](https://mehomeai.replit.app/me/).

## Status

Implemented on review branch `feat/home-agent-foundation`
(committed/pushed; draft PR #1 pending review/checks). No merge to
protected `main`; no deployed release. Agent runtime
(`runtime-bridge-v1` + `task-rpc-v1`): the agent loop is owned by the
maintained LangGraph prebuilt ReAct harness pinned `langgraph==1.2.19` /
`prebuilt==1.1.1` with persisted SqliteSaver checkpoint (`create_react_agent`
is the deprecated-upstream name kept as the compatible 1.x checkpoint alias,
strict allowlist serde + legacy 0.6 resume). Nine brokered tools + four
delegation tools only — no framework default filesystem/shell/subagent tools.
Household data and execution stay on the home host; local Ollama
(`http://127.0.0.1:11434`) is the default. An optional remote provider is
explicitly enabled only with `ME_RUNTIME_ALLOW_REMOTE=1` plus its key, and
then the scoped prompt/history/tool context goes to that provider (synthetic
prompts only for live checks, never household data). Strict live Muse final
passed (response-block extract; parent ORBIT/English + 5-byte child + one
assistant; 47.5s) — no further live calls.
`run_code` is Docker one-shot only (no host fallback): bounded stdout/stderr
streams are the durable artifacts; container files are ephemeral tmpfs and
never collected via `docker cp`. `browser_open` is the real isolated
interactive Playwright browser (dedicated `--network none` container via the
vetted broker; readonly needs no approval, click/fill/press gate on
`browser_act`); missing Docker/image fails closed with
`prerequisite_missing`. Model memory means persisted
conversation/checkpoints scoped to owner/conversation/run — no semantic-RAG,
cross-conversation retrieval, MCP, or voice-calls production claims.
Verified (finals, see `backend-server/docs/final-validation.md`): runtime
full **122 pass / 4 skip**, targeted context+response **29 pass**, actual
home+runtime runners **9 pass**. No consumer-production/HA claims.

## Scope

Agent execution, tool permissions, orchestration, and model integration.

## What it does

- Nine brokered tools, default deny (`EXPECTED_TOOL_NAMES` asserted at
  build): `files_read/list/write`, `web_fetch` (curl-backed broker with
  allowlist, per-hop DNS pinning, redirect/body caps), `browser_open`
  (limited offline renderer only — JS disabled, subrequests aborted),
  `run_code` (Docker one-shot only, no host fallback), `animation_build`
  (Pillow GIF + editable scene), `ask_user` / `request_approval` (pause for
  home input).
- Local-first models via `langchain-ollama`; `openai`/`anthropic` adapters
  require `ME_RUNTIME_ALLOW_REMOTE=1` plus the key env var, even for
  localhost URLs. `use_responses_api: true` selects the Responses API
  (best-effort with stderr note on fallback).
- Versioned stdin/stdout bridge (`runtime-bridge-v1`) for the home service;
  full contract in [docs/runtime-bridge-v1.md](docs/runtime-bridge-v1.md).

## Run the fixture suite (offline, no keys)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/python -m pytest tests --ignore=tests/test_live_optin.py -v
```

`tests/test_live_optin.py` is opt-in only and skipped by default
(synthetic prompts, explicit env + credentials required). Docker,
Playwright/Chromium, Ollama, and live-model runs are manual gates on the
operator's host — absent backends report honest
`prerequisite_missing` / `model_not_configured` failures.

## End-to-end with the home service

The Node home service (`infra-server`) spawns this runtime per task; see the
authoritative operator runbook at
[`infra-server/docs/end-to-end-runbook.md`](../infra-server/docs/end-to-end-runbook.md)
(§§1–4 lifecycle, §6 exact per-repo commands with final counts, §7
live-model coordination). Overall test evidence:
`backend-server/docs/final-validation.md`. No full-production, native-app,
native-build, or HA claims; Docker images / local Ollama models are manual
operator provisions.

## Product architecture and reuse work

The agent executes on the user's separate home computer alongside `infra-server`,
which hosts household data and multiple users/conversations. `backend-server`
coordinates accounts/home access/activation tickets only (no private chat
relay/storage). Setup provisions a named-tunnel record + coordinator
activation; phones use the direct paired HTTPS home URL from the trusted
local `home-pairing-v1` invitation (cloud `endpointHint` is discovery only,
never a trust anchor). `setup-infra` installs and maintains the home environment.

Start with [product architecture](docs/product-architecture.md), the
[whole-product delivery plan](docs/product-delivery-plan.md), the
[reuse audit](docs/reuse-audit.md), and the implemented
[runtime bridge contract](docs/runtime-bridge-v1.md).

## Contributing

Start with the [contribution guide](https://github.com/mehomeai/.github/blob/main/CONTRIBUTING.md),
[code of conduct](https://github.com/mehomeai/.github/blob/main/CODE_OF_CONDUCT.md), and
[governance](https://github.com/mehomeai/.github/blob/main/GOVERNANCE.md).
Discuss substantial changes in an issue before implementing them. Pull requests
should include relevant tests and documentation once application code exists.

For questions and cross-project planning, use [community discussions](https://github.com/mehomeai/.github/discussions).
Report vulnerabilities privately through this repository's **Security → Report a vulnerability**;
see the [security policy](https://github.com/mehomeai/.github/blob/main/SECURITY.md).

## License

[Apache License 2.0](LICENSE). Third-party code, model weights, and datasets may
have their own terms and must be reviewed before inclusion.
