# Reuse audit and first integration evidence

Status: source audit plus executed model-free home-worker spike, 2026-11-02
(America/Los_Angeles). Inspected commits may be dated 2026-11-03 UTC. No final
production framework, database, phone framework, or hardware selection is implied.

Requirements: [product architecture](product-architecture.md) and
[whole-product delivery](product-delivery-plan.md): user-owned home execution and
data, multiple private household users/conversations, hosted Me coordination,
phone interaction, and installation/upgrade tooling.

## Recommendation

Frontend update: the user has supplied an existing Expo prototype at
`Personal-AI-Showcase-3/artifacts/me-mobile`. Use that as the phone UI baseline;
OpenMuse is now a possible source of individual missing components and backend
modules, rather than the default mobile application to adopt. The `phone-app`
repository records the inspected screens and integration gaps in
`docs/frontend-prototype.md`.

Reuse existing software at the largest boundary that fits the household privacy
and deployment model; keep Me adapters separate from imported source.

- **OpenClaw is a strong complete home-assistant baseline to trial next.** Its
  gateway, agent/session scopes and protocol may replace substantial runtime and
  infrastructure work. Separate agent workspaces do not establish hard user
  isolation. A per-user service boundary is an option to measure.
- **OpenMuse is a concrete mobile/workflow donor.** Its full application's
  required external thread service and single-owner login require adaptation for
  Me. Reuse compatible modules without inheriting those assumptions. Its task
  worker is the first executed slice.
- **Deep Agents is the embeddable-harness alternative** if integrating a complete
  gateway adds more complexity than it saves. It provides agent-loop behavior;
  home identity/data/client integration remains work for Me.

These are source-based engineering assessments. No live-model benchmark between
the complete products was run; there is no measured overall winner.

## Pinned source inventory

| Source | Inspected revision | Reuse opportunity | Main integration gap |
| --- | --- | --- | --- |
| [CopilotKit/openmuse](https://github.com/CopilotKit/openmuse/tree/a4944f4c471d3dc93b8bfc060b5da309b5216ab2) | `a4944f4c471d3dc93b8bfc060b5da309b5216ab2` | Native UI, task/approval views, worker, browser/computer adapters | Single-owner auth, external chat storage, durable follow-ups, household resource policy |
| [openclaw/openclaw](https://github.com/openclaw/openclaw/tree/9312f263af4a3095d11f059e8637acf427701a96) | `9312f263af4a3095d11f059e8637acf427701a96` | Home gateway, sessions/agents, protocol, tools | Trusted gateway boundary versus private family users; sandbox and plugin/account state |
| [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents/tree/5829691de8f43d201f9534fb731643223c0bbccc) | `5829691de8f43d201f9534fb731643223c0bbccc` | Embeddable planning/tool/context/skill harness | Household data, home API, installation and phone integration |

The inspected projects identify their relevant project code as MIT-licensed;
services, model weights, optional dependencies and assets need their own review.
Preserve original notices. This is not blanket clearance for a complete product.

## Source findings

OpenMuse's [thread documentation](https://github.com/CopilotKit/openmuse/blob/a4944f4c471d3dc93b8bfc060b5da309b5216ab2/docs/RICH-THREADS.md)
requires CopilotKit Intelligence in every mode, describes a single-user workspace,
and places chat follow-up queues in the open client. Server-side delegated tasks
are separate. Replacing chat persistence requires engineering, not just a flag.

Its [computer documentation](https://github.com/CopilotKit/openmuse/blob/a4944f4c471d3dc93b8bfc060b5da309b5216ab2/docs/COMPUTER.md)
describes different Docker/E2B lifecycle and network boundaries. The cloud adapter
is not Me's default home executor; reused tools must obey home permissions.

OpenClaw's [multi-agent documentation](https://github.com/openclaw/openclaw/blob/9312f263af4a3095d11f059e8637acf427701a96/docs/concepts/multi-agent.md)
defines per-agent scopes but explains that workspace paths are not sandboxes and
plugin storage may remain global. Its [gateway protocol](https://github.com/openclaw/openclaw/blob/9312f263af4a3095d11f059e8637acf427701a96/docs/gateway/protocol.md)
is an integration surface to examine before writing an equivalent protocol.

Deep Agents' [README](https://github.com/langchain-ai/deepagents/blob/5829691de8f43d201f9534fb731643223c0bbccc/README.md)
distinguishes its harness from LangGraph's runtime. Select one agent-loop owner;
do not combine overlapping engines by default. The inspected package requires
Python 3.11 or later; OpenClaw declares specific Node 24/26 ranges. Validate the
actual selected release on declared home hardware.

## Reused code and executed evidence

`infra-server/vendor/openmuse` contains the actual unchanged task worker, logging
helper and MIT license from the OpenMuse pin. An inventory records exact hashes.
The worker runs against a small built-in Node SQLite adapter in
`infra-server/spikes/openmuse`. That is integration glue, not a new scheduler or
the production storage implementation. Upstream's PostgreSQL store remains an
available production integration option once its dependencies are installed.

Eight integration tests passed: owner/conversation-scoped records across two
users/four chats, input waits while other work completes, reopening persistence,
competing worker claims, real child-process crash recovery after lease expiry,
approval eligibility, cancellation/stale-writer fencing, and failure recording.
Some behaviors share a test; there are eight executable cases, not eight fully
implemented product features. The synthetic home demo also passed.

Eight Python tests passed for source hashes, immutable pins, fixed paths,
download staging, and candidate-bundle validation. These checks run offline.

Limits: owner identity is trusted fixture input; conversation IDs are labels.
This does not prove authentication, authorization, full conversation storage,
model quality, phone/backend relay, real approvals, browser/code/render workers,
full upstream build/typecheck, Docker, or installation. The npm registry was
unreachable from the shell and Docker was not on PATH; those results were not
invented or inferred from the narrower test.

The imported worker caps in-flight work at three, scans task records, keys local
active work by task ID, and does not supply Me's household fairness/resource
classes or a validated dependency graph in this slice. Task IDs must be unique
across owners; the fixture uses home-generated UUIDs. Lease recovery cannot make
arbitrary external side effects exactly once. These are production review items.

## Reuse across Me repositories

| Repository | Prefer to reuse | Me-specific integration |
| --- | --- | --- |
| `phone-app` | User's existing Me Expo prototype; external components only for identified gaps | Real pairing/identity, home-backed state, authorized task events/results |
| `backend-server` | Authentication, transport and notification libraries | Account-to-home registration, routing, privacy boundaries |
| `infra-server` | Gateway/runtime where suitable, workers, storage/service tooling | Home household data/access, resource policy, backend connection |
| `agentic-framework` | One harness, browser/coding/creative tools | Scoped capability composition and completion checks |
| `setup-infra` | Packaging/service/isolation/backup mechanisms | Supported configuration, pairing, upgrades and recovery |
| `feed-algorithm` | Applicable ranking/retrieval components | Family goal controls and authorized inputs |
| `web-app` | Existing website/demo | Accurate supported features, hardware/setup and onboarding |

The spike does not obligate production to run OpenMuse and OpenClaw schedulers
together. If a complete gateway owns execution, use its lifecycle or one explicit
adapter. Disposable experiments should reduce custom code, not add permanent
duplicate services.

## Upstream integration included

`infra-server` now has hash verification, fixed-file update tooling, compatibility
CI and a weekly candidate workflow; existing Actions Dependabot tracking is retained. Tests run
under read-only permissions. A separate publisher validates candidate data and
can open a review PR without executing imported code under its write token.

Only adopted files refresh automatically. PRs link the full upstream comparison
for feature discovery; new files/tools/dependencies/permissions need explicit
adoption. No automatic merge, home deployment, or production upgrade exists.
Workflows remain local until committed/pushed and merged. Repository settings
must permit automated PR creation. The precise mechanism and limitations are
documented in `infra-server/docs/upstream-updates.md`.

## Next integrated trial

Use the same home user/session/task contract to trial a complete home gateway
against an embeddable harness. Require two users/multiple chats, a persisted
question alongside independent work, a real model/tool output, restart/reconnect
recovery, and denied cross-user access. Measure hardware and adaptation effort
before selecting the production base.

In parallel, prototype hosted-backend ↔ home messaging and the conversation/data
service. Reuse an existing gateway protocol if it meets Me's identity/privacy
boundary. Adapt existing phone components after that contract stabilizes.
