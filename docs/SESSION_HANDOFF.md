# Session Handoff — agentic-framework (2026-11-03)

> WIP draft on `feat/upstream-agent-infrastructure`. NOT main / production-ready. Original migration paused; only publication handoff complete. Parent owns all Git.

- Central handoff: https://github.com/mehomeai/.github/blob/feat/upstream-agent-infrastructure/docs/agent-infrastructure-handoff-2026-11-03.md (note: `../.github` is wrong from `docs/`; restore clones `.github` as `me-github`)
- Machine-readable: https://github.com/mehomeai/.github/blob/feat/upstream-agent-infrastructure/docs/agent-infrastructure-checkpoint/agent-infrastructure-checkpoint-2026-11-03.json
- Immutable snapshot (preserve): https://github.com/mehomeai/.github/blob/feat/upstream-agent-infrastructure/docs/agent-infrastructure-checkpoint/git-snapshot.json
- This repo branch: `feat/upstream-agent-infrastructure` (WIP draft)
- Published PR (unchanged): https://github.com/mehomeai/agentic-framework/pull/2 @ `3dadb75dd3ceb4854466b0b15f2680904fe69db7`
- Pin: OpenBot `6bfee780c8e8d05141375ea55ebde33eefb3dbc0`; port 4206 POST `/`; local Ollama needs non-secret `OPENAI_API_KEY=ollama` (`runtime-local-model-contract`).
- Reported: 133 pass/4 skip entrypoint+suite run; Linux CI image/health/auth/lock green (runs 41735540383 / 41735539610 / 41735539612); Ollama fix 17 targeted pass uncommitted.
- Resume blocker (setup-infra): actual unittest ran 166, FAILED 3 (`test_imported_dependency_tamper_fails_closure`, `test_generic_key_satisfies_native_check`, `test_operational_ready_reachable_without_backup_claims`); suite NOT passing.
- Needs review: image recipe/CIs fixed here but Linux build gate stays with parent; container Ollama reachability must prove CONTAINER namespace.
- Parent all-8 repository-health checks pass. Final post-push CI pending / not awaited.
- Resume: clone + checkout per central restore snippet → read central doc → narrow OpenCode fixes → Linux acceptance. Mandate: `opencode run` default model only.
