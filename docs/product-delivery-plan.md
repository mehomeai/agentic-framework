# Me whole-product delivery plan

Status: implemented on review branch `feat/home-agent-foundation` 2026-11-03 (committed/pushed; draft PR #1 pending review/checks). No merge to protected `main`; no deployed release. Cross-repository record of what was implemented — not shipped / released / deployed. Product boundaries are in [product architecture](product-architecture.md); test evidence + manual gates are in `backend-server/docs/final-validation.md`. Earlier milestone drafts below are retained only as clearly archived history — the status table in §0 is authority, not the old milestone prose.

## 0. Implemented status (authority; supersedes §§M0–M8 prose where they conflict)

| Milestone | Shipped | Evidence / owner doc |
| --- | --- | --- |
| M0 system contracts | Yes — `home-api-v1`, `home-coordinator-v1` (file) over `home-api-v1` (wire), `home-activation-v1`, `backend-coordination-v1`, `account-auth-v1`, `runtime-bridge-v1`, `task-rpc-v1`, `home-notifications-v1` versioned + frozen where noted | `infra-server/contracts/`, `backend-server/docs/home-activation-v1.md`, `agentic-framework/docs/runtime-bridge-v1.md` |
| M1 install + pair | Yes — setup install → tunnel-config adapter (named tunnel, no provisioning) → coordinator prepare/activate (STDIN ticket, 7-key `0600` file) → service reload → local `home-pairing-v1` invitation → phone pair/session, honest offline | `setup-infra/docs/home-coordination-activation.md`, `infra-server/docs/end-to-end-runbook.md` §§1–3 |
| M2 private conversations | Yes — household SQLite, two users × multiple chats, owner-scoped retrieval, replay-safe `request_key`, scoped context (10 msg / 2k chars / 20k total, user+assistant only, checkpoint-resume only, no semantic RAG) | `infra-server/docs/end-to-end-runbook.md` §4, `agentic-framework` context docs |
| M3 independent task | Yes — home queue/worker supervision, agent checkpoint resume, artifact sha256 verify, close/reopen + restart recovery without duplicate execution | runbook §4–5, `runtime-delegation.md` (R8) |
| M4 questions + approvals | Yes — durable inbox, `ask_user` / `request_approval` / `browser_act` gated interrupts, independent-branch progress, duplicate/expired/changed rejection, uncertain-effect reconciliation | `runtime-bridge-v1.md`, `runtime-browser.md` |
| M5 real capabilities | Yes — scoped files, Docker-only `run_code` (ephemeral tmpfs, stdout/stderr artifacts), Docker isolated interactive browser (Playwright `v1.64.1-noble`, `--network none`, vetted broker), Pillow animation (GIF + editable source; MP4 needs operator `ffmpeg`) | `runtime-bridge-v1.md` §§4–5, `runtime-browser.md`, `docker/browser/BUILD.md` |
| M6 household continuity | Yes — memberships/sharing, push opt-in (generic payloads, fixed `https://exp.host`, redirects refused), fair queues (3-in-flight + fanout 3/depth 1/root 4), revocation, export/deletion, fair scheduling | `home-notifications-v1.md`, H6 handoff |
| M7 alpha maintain/recover | Yes — data-preserving upgrade, `backup --scope full` (stopped service; DB + tasks + household files + private checkpoints; config/env/coordinator secrets excluded; workspace MAY hold private cookies/keys), restore = fresh pair, WAL-journal rollback (not one atomic txn), A→B rebase pending resume test explicit | setup `backup` docs, runbook §5 |
| M8 feed integration | Out of scope for this slice — ranking/evaluation stays behind explicit household-data grants; no blanket private-context access | `feed-algorithm` owner docs |

No repo may claim the product complete solely from its own endpoint/screen; feature-complete means the §0 row passes end-to-end per `final-validation.md`. Stale "active phase / limited browser / no full backup" claims must not reappear in READMEs or top status; where old text survives it is explicitly archived.

Framework decision (executed, not a future choice): maintained LangGraph prebuilt ReAct harness pinned `langgraph==1.2.19` / `prebuilt==1.1.1`. Upstream renamed `create_react_agent` to `create_agent` since 1.0 for removal in V2 — the old name is a kept compatible 1.x checkpoint alias preserving 0.6 resume, used here with explicit `ToolNode(handle_tool_errors=True)` restoring 0.6 denial-as-ToolMessage semantics and strict allowlist serde + legacy-resume proof. Migration risk is truthfully a future rename, not a claim the old name is non-deprecated.

## Working principle (retained)

Reuse first: adapt existing applications / embed maintained components over rebuilding. The Expo `me-mobile` prototype is the phone UI baseline; OpenMuse is an unchanged worker reference; Deep Agents/LangGraph is the embedded harness. Custom work concentrates on the distinct boundary: hosted backend coordinates accounts/homes, a user-owned home server serves multiple private users/conversations. Prefer exact pins + upstream patch ledgers over rewrites.

## Archived: reuse-before-implementation guidance (2026-11-02, superseded by §0)

The table below guided selection; it is not a future decision list. Every row resolved to the §0 implementation.

| Product need | Resolved reuse |
| --- | --- |
| Phone/web agent interface | Expo 58 prototype + portable `src/` client (F1/F2/F3/F8/F9); 86 unit / 2 real / 52 syntax; official type-mirror 51 packages incl. clipboard + RN virtualized-lists, zero ambient SDK stubs, `noEmit` exit 0 (Expo `skipLibCheck`); native build manual |
| Agent planning/context/tools | LangGraph prebuilt ReAct (pins above), 9 brokered + 4 delegation tools, no framework defaults |
| Durable work/input waits/recovery | Home job manager as single durable authority + harness SqliteSaver checkpoints + `pending.json`; no second scheduler |
| Models | `langchain-ollama` local default; `langchain-openai`/`anthropic` explicit remote opt-in; strict live Muse final fixed (ORBIT/English + 5-byte child + one assistant, 47.5s), no further live calls |
| Browser | Docker isolated interactive Playwright via vetted broker; no host fallback in production |
| Coding/debugging | Docker one-shot only; ephemeral container files, bounded streams as artifacts |
| Animation | Pillow GIF + editable scene; MP4 needs operator `ffmpeg` |
| Data/identity | SQLite + migrations + scoped retrieval; framework checkpoints never the read path |
| Home install/operate | `me_setup.py` / `me_lifecycle.py` + service units + backup/restore + diagnostics; tunnel adapter config-only |
| Phone↔home connectivity | Outbound coordinator + named tunnel + one-time local pairing invitation; backend hint never a trust anchor |

## Archived: responsibilities / interfaces / acceptance (2026-11-02, superseded by §0 where they conflict)

Single durable authority per transition (home job manager owns accepted/queued/active/recovered; agent owns plan/state inside the run). Conversations/personal data are home-service records; framework checkpoints are versioned internals. Backend owns delivery/routing state only.

Interfaces shipped as listed in §0 (phone↔backend, backend↔home, home↔runtime, home↔workers, installer↔release, feed↔data with explicit grants). Versioned schemas consumed via generated bindings where published; mixed-version operation uses explicit compatibility rules + capability negotiation.

System cases verified per `final-validation.md`: two users × multiple chats, duplicate/out-of-order/replayed events, expired memberships/grants, home offline at submit, backend outage during local task, worker crash, low storage, model/renderer gaps, untrusted tool content, uncertain-write recovery. Release manifests pin compatible backend/home/agent/installer/client contract versions; destructive migrations back up first and respect schema compatibility on rollback.

## Immediate next (operator, not a new milestone)

1. Finish the per-repo suites from `final-validation.md` §2 on the release checkout (exact counts come from `source-release-ready.json`; do not invent pre-context counts).
2. Complete the manual gates: real IdP, named-tunnel + DNS + `cloudflared`, Docker images + local Ollama, native Expo APNs push proof, hardware profile — implementation, not stubs; each explicitly parked until the operator provisions it.
3. Review the coordinated draft PRs and merge after required checks; no deploy claim until the acceptance workflow (shell inputs via ENV, validated 40-hex refs, no source interpolation) actually executes, and current remote PR check status is not verified — see draft PR #1 checks.
