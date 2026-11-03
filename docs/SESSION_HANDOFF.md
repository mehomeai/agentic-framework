# Session Handoff — agentic-framework (2026-11-03)

> WIP draft on `feat/upstream-agent-infrastructure`. NOT main / production-ready. Implementation paused for machine move; migration NOT complete. Parent owns all Git.

- Central handoff: `../.github/docs/agent-infrastructure-handoff-2026-11-03.md`
- Machine-readable: `../.github/docs/agent-infrastructure-checkpoint/agent-infrastructure-checkpoint-2026-11-03.json`
- This repo branch: `feat/upstream-agent-infrastructure` (WIP draft)
- Published PR: https://github.com/mehomeai/agentic-framework/pull/2 @ `3dadb75dd3ceb4854466b0b15f2680904fe69db7`
- Pin: OpenBot `6bfee780c8e8d05141375ea55ebde33eefb3dbc0`; port 4206 POST `/`; local Ollama needs non-secret `OPENAI_API_KEY=ollama` (`runtime-local-model-contract`).
- Reported: 133 pass/4 skip entrypoint+suite run; Linux CI image/health/auth/lock green (runs 41735540383 / 41735539610 / 41735539612); Ollama fix 17 targeted pass uncommitted.
- Needs review: image recipe/CIs fixed here but Linux build gate stays with parent; container Ollama reachability must prove CONTAINER namespace.
- Resume: clone + checkout per central restore snippet → read central doc → narrow OpenCode fixes → Linux acceptance. Mandate: `opencode run` default model only.
