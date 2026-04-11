# Theo Implementation Plan -- Overview

20 phases. Each is incremental, testable, and completable in one Opus 4.6 (1M) context window.

## Dependency Graph

The dependency graph is **strictly linear** after Phase 3a, with 12a reordered after 14 to
resolve the circular dependency between the executive loop and the subagents it dispatches.
13b lands after 12a because it introduces reflexes, ideation, and proactive proposals that
build on goal-state projections and autonomy domains.

```text
Phase 1: Foundation (config, pool, migrations, errors)
    |
Phase 2: Event Types (unions, IDs, upcasters)
    |
Phase 3: Event Log & Bus (table, store, dispatch, checkpoints, replay)
    |
Phase 3a: Test Database Isolation (theo_test DB, test-db recipe)
    |
Phase 4: Memory Schema (node, edge, episode, core_memory, user_model, self_model, skill)
    |
Phase 5: Embeddings & Knowledge Graph (nodes, edges, temporal versioning)
    |
Phase 6: Episodic & Core Memory (episodes, 4-slot core, changelog)
    |
Phase 7: Hybrid Retrieval / RRF (vector + FTS + graph fusion)
    |
Phase 7.5: Bootstrap Identity (persona seed, goals seed, prompt template)
    |
Phase 8: User Model, Self Model & Privacy Filter
    |
Phase 9: MCP Memory Tools (tool server, all memory tools)          [SHIPPED]
    |
Phase 10: Agent Runtime (context assembly, SDK query, hooks, sessions,
          external content envelope threading, advisor-assisted dispatch)
    |
Phase 11: CLI Gate (interactive CLI, streaming, owner commands)
    |
Phase 12: Scheduler (jobs, cron, priority classes, built-in jobs)
    |
Phase 13: Background Intelligence (contradiction detection, auto-edges, consolidation,
          forgetting, propagation, abstraction, decision/effect handler split)
    |
Phase 13a: Memory Resilience (episode salience, recency RRF, kind decay, provenance)
    |
Phase 14: Subagents, Onboarding & Engine Lifecycle (8 subagents with advisor, psychologist
          onboarding, Engine state machine, SkillRepository)
    |
Phase 12a: Goal Loops & Executive Function (BDI executive, graph-native goals, lease, poison
           quarantine, intention reconsideration, operator commands)
    |
Phase 13b: Autonomous Ideation & Reflexes (webhooks, reflex class, ideation job, proposals,
           egress privacy filter, causation-chain trust propagation)
    |
Phase 15: Operationalization (launchd, self-update, observability, logging)
```

**The reordering from the original draft**: phases 12a and 13b were originally placed
between 12 and 13 (in that order), but the agentic-AI review surfaced that 12a depended on
Phase 14's subagent definitions and on Phase 13's background intelligence (contradiction
detection in particular, because intention reconsideration hooks into it). The corrected
order is above. File names retain their `a` / `b` suffixes so history is preserved; the
dependency arrows tell the truth.

## Phase Summary

| # | Phase | Key Deliverables | Est. Lines | Risk |
| --- | ------- | ------------------ | ----------- | ------ |
| 1 | Foundation | Config (zod), pool (postgres.js), migration runner, `Result<T,E>` errors | ~600 | Low |
| 2 | Event Types | `TheoEvent` union, ULID `EventId`, upcaster registry, `CURRENT_VERSIONS` | ~800 | Low |
| 3 | Event Log & Bus | Partitioned events table, `EventBus.emit()` (write + dispatch), handler checkpoints, replay, tx support | ~1200 | Medium-High |
| 3a | Test DB Isolation | Separate `theo_test` database, Docker init script, `just test-db` recipe | ~50 | Minimal |
| 4 | Memory Schema | Single migration: node, edge, episode, core_memory, user_model, self_model tables, skill table, access_count/last_accessed_at on node, pattern/principle NodeKinds | ~600 | Low |
| 5 | Embeddings & Knowledge Graph | Local ONNX embeddings, `NodeRepository` (CRUD, adjustConfidence, findSimilar), `EdgeRepository`, temporal versioning | ~1200 | Medium |
| 6 | Episodic & Core Memory | `EpisodicRepository`, 4-slot `CoreMemoryRepository`, changelog, atomic state+event writes | ~700 | Low |
| 7 | Hybrid Retrieval | Single-query RRF fusing vector + FTS + graph, configurable weights, importance-weighted RRF, access tracking on retrieval | ~600 | High |
| 7.5 | Bootstrap Identity | Persona seed, goals seed, `buildPrompt()` template, external content envelope rule, onboarding detection | ~400 | Low |
| 8 | Models & Privacy | `UserModelRepository` (+ `egress_sensitivity` field), `SelfModelRepository` (+ `autonomy_level`), `PrivacyFilter` upgraded to take `effectiveTrust` argument | ~1000 | Medium |
| 9 | MCP Memory Tools | **(SHIPPED)** store_memory, search_memory, search_skills, read_core, update_core, link_memories, update_user_model | ~500 | Low |
| 10 | Agent Runtime | Context assembly from memory tiers, SDK `query()` integration (v0.2.92 verified), hooks, session management, cache-optimized prompt ordering, smart session management, effective-trust threading into tool metadata, advisor settings dispatch | ~2000 | High |
| 11 | CLI Gate | Interactive terminal gate, streaming responses, ephemeral events, operator commands (`/goals`, `/proposals`, `/autonomy`, `/consent`, `/audit`, `/redact`) | ~800 | Low |
| 12 | Scheduler | Job store, cron runner, priority-class scheduler (4 classes with preemption via AbortController), built-in jobs (consolidation, reflection, scanning) | ~1300 | Medium |
| 13 | Background Intelligence | Contradiction detection (split into requested/classified), auto-edges, consolidation with node merging, forgetting curves, importance propagation, abstraction hierarchy synthesis, **decision/effect handler mode amendment to Phase 3's bus** | ~1400 | Medium |
| 13a | Memory Resilience | Episode salience scoring, recency RRF signal, node metadata, windowed self-model calibration, kind-specific decay, node provenance, topic-level consolidation | ~600 | Low-Medium |
| 14 | Subagents, Onboarding & Lifecycle | 8 subagent `AgentDefinition`s (with `advisorModel` where applicable), psychologist-led onboarding, `Engine` state machine, reflector creates/refines skills, full `SkillRepository` | ~1200 | Low-Medium |
| 12a | Goal Loops & Executive | Graph-native goals, `GoalEvent` union (21 types), `goal_state`/`goal_task` projections, lease mechanism, poison quarantine, intention reconsideration, `read_goals` MCP tool, operator commands | ~1800 | High |
| 13b | Ideation & Reflexes | Webhook gate (tunnel-only public), HMAC with `timingSafeEqual`, reflex decision/effect split, ideation with replay determinism + provenance filter + advisor caching, proposal lifecycle with TTL/GC, causation-chain effective trust walker, egress privacy filter, degradation ladder | ~2200 | **Critical** |
| 15 | Operationalization | `launchd` plist, structured logging (JSON + file rotation), OTel tracing + metrics (incl. advisor iterations, reflex dispatch, ideation cost, degradation level), self-update with rollback, workspace layout | ~1300 | Medium |

**Total: ~21,350 lines across 20 phases.** (Original estimate was ~16,300; the 12a/13b
rewrites add ~5,000 lines because they carry complete event catalogs, full SQL, full test
plans, comprehensive Risks, and the cross-cutting cognitive architecture they depend on.)

## Systemic Decisions

These cross-cutting decisions affect multiple phases and are documented here to avoid
repetition. Several new decisions were added after the 12a/13b security review; they are
also documented in full in `docs/foundation.md §7 Autonomous Agency`.

### 1. Event+Projection Atomicity via Bus Transaction Parameter

The event bus `emit()` accepts an optional `{ tx }` parameter. When provided, the event is
written inside the caller's SQL transaction, guaranteeing that the event and the projection
update either both commit or both roll back. Used by: Phase 3 (bus design), Phase 5
(node/edge mutations), Phase 6 (core memory updates), Phase 12a (goal projection), Phase 13
(node merging), Phase 13b (proposal lifecycle).

### 2. Streaming Architecture

The data flow for a user message: SDK `query()` runs the agent loop → the engine consumes
the async generator → ephemeral events are emitted for intermediate state (tool calls,
thinking) → the gate receives events and streams them to the user. Ephemeral events are
typed separately in the event union and are never persisted to the event log.

### 3. SDK API Verified Against v0.2.92

All SDK integration code uses the verified API surface:

- `query()` returns `AsyncGenerator` — must be consumed with `for await`
- `SDKResultSuccess` has `structured_output?: unknown` for JSON schema output
- `AgentDefinition` type used for subagent configuration (Phase 14)
- `options.settingSources: []` for prompt isolation
- `options.permissionMode: 'bypassPermissions'` for non-interactive calls
- `options.settings.advisorModel` (via the CLI settings layer) enables the server-side
  advisor tool with beta header `advisor-tool-2026-03-01` (Phase 14 subagents; Phase 12a
  executive dispatch; Phase 13b ideation)
- `usage.iterations[]` is read for per-iteration cost accounting (executor iterations are
  `type: "message"`, advisor iterations are `type: "advisor_message"`)

### 4. GENERATED BY DEFAULT for IDs

All auto-increment primary keys use `GENERATED BY DEFAULT AS IDENTITY`. This allows event
replay to insert rows with explicit IDs (overriding the default) while still
auto-generating IDs during normal operation. `GENERATED ALWAYS` would reject explicit IDs
during replay.

### 5. Context Caching via Stable Prompt Ordering

The system prompt is ordered from most-stable to most-volatile content to maximize
Anthropic API cache hits: Static Instructions (including the external content envelope
rule) + Persona (rarely changes) → Active Skills + Goals + User Model (session-level) →
Current Context + Relevant Memories (per-turn). Two explicit cache breakpoints divide the
prompt into three zones. Used by: Phase 7.5 (`buildPrompt` ordering), Phase 10 (context
assembly).

### 6. BDI Cognitive Architecture

Theo is a BDI (Belief–Desire–Intention) agent with dual-process execution. Beliefs live in
the knowledge graph, episodic memory, and user model. Desires live in `core_memory.goals`
(priority stack) and active `NodeKind = 'goal'` nodes. Intentions live in the `goal_state`
projection (phase 12a execution state). Dual-process: System 1 = reflex handler (phase
13b), System 2 = executive loop (phase 12a), Offline consolidation = phase 13 + ideation.
Full definition in `docs/foundation.md §7.1`.

### 7. Causation-Chain Effective Trust Propagation

Every durable event stores `effective_trust_tier = min(actor_trust, parent.effective_trust)`
computed at emission time by walking `metadata.causeId` up to a bounded depth (default 10).
The privacy filter's `checkPrivacy()` signature takes `effectiveTrust`, not the actor's raw
tier. Memory repositories enforce the cap at the write boundary. This prevents trust
laundering across causation hops (webhook → graph node → ideation → goal → subagent write).
Full definition in `docs/foundation.md §7.3`. Applied by: Phase 8 (filter upgrade), Phase
12a (goal creation), Phase 13b (reflex/ideation).

### 8. Decision / Effect Handler Modes

Phase 3's bus is amended by Phase 13 to support `HandlerMode = "decision" | "effect"`.
Decision handlers are pure over event data and run on both live dispatch and replay. Effect
handlers call the outside world (LLMs, git, network) and run only in live mode. Phase 3's
replay path skips `effect` handlers; the external result reaches downstream decision
handlers via a captured event (e.g., `contradiction.classified`, `ideation.proposed`,
`reflex.thought`). Full definition in `docs/foundation.md §7.4`.

### 9. Priority Class Scheduler

Phase 12's scheduler is extended with four priority classes: `interactive` >
`reflex` > `executive` > `ideation`. Preemption is via `AbortController`; a preempted class
has 2 s to emit a `*.yielded` event and drain before force-abort. Each class has a bounded
queue with distinct overflow behaviors (coalesce, defer, drop). Degradation level shifts
which classes are allowed to run. Full definition in `docs/foundation.md §7.5`.

### 10. External Content Envelope

External (webhook, email, untrusted) content is wrapped in nonce-delimited
`<<<EXTERNAL_UNTRUSTED_{nonce}>>>` blocks. The static system prompt (cached) contains a
single authoritative instruction: content inside such blocks is data, never instructions,
no matter what trust-claiming language appears inside. External-tier turns run with a
restricted tool allowlist (read-only memory tools). Full definition in
`docs/foundation.md §7.6`. Applied by: Phase 10 (chat engine), Phase 13b (reflex dispatch),
Phase 12a (external-trust goal turns).

### 11. Autonomy Ladder

Per-domain autonomy levels (0 Suspend → 5 Act-silently) stored in `autonomy_policy` table.
Seeded defaults in Phase 12a migration. Ideation-origin goals are hard-capped at level 2
regardless of domain setting. Calibration gate: autonomy level is honored only when the
self-model's calibration for the domain exceeds 0.9 over ≥ 20 samples. Denylist paths
(`.env*`, `src/memory/privacy.ts`, outbound HTTP, etc.) are never bypassed by any level.
Full definition in `docs/foundation.md §7.7`.

### 12. Egress Privacy Filter

Every user-model dimension has an `egress_sensitivity` (`public` / `private` /
`local_only`). Outgoing prompts are filtered at the `query()` call site based on turn
class: interactive turns include `private` dimensions, autonomous turns (reflex, executive,
ideation) do not, `local_only` is never sent to the cloud. A consent ledger
(`consent_ledger` table) requires an active `policy.autonomous_cloud_egress.enabled` event
before any autonomous cloud turn. Every non-interactive cloud call emits `cloud_egress.turn`
for audit. Full definition in `docs/foundation.md §7.8`.

### 13. Advisor-Assisted Execution (Anthropic beta)

The Claude Messages API advisor tool (`advisor_20260301`, beta header
`advisor-tool-2026-03-01`) is exposed via the SDK as `options.settings.advisorModel`.
Subagents whose work is plan-then-execute (`planner`, `coder`, `researcher`, `writer`,
`main`, `ideation`, `reflector`) are configured with Sonnet executors + Opus 4.6 advisor,
near-Opus quality at near-Sonnet cost. Subagents whose work is reflex-speed (`scanner`,
`consolidator`, `contradictor`) do not use the advisor. Cost accounting sums
`usage.iterations[]` (executor iterations billed at executor rate,
`type: "advisor_message"` iterations billed at advisor rate). Under degradation level ≥ L1
the advisor is progressively dropped. Full definition in
`docs/foundation.md §4 Advisor-Assisted Execution`.

## Event Catalog

Events added across all phases. Phase 12a and 13b dominate the list because they introduce
the autonomous agency events; the counts for each group are noted.

### Chat (phase 10 — 8 events)

| Event Type | Actor | Purpose |
| ---------- | ----- | ------- |
| `message.received` | user | Incoming user message |
| `turn.started` | theo | Agent turn begins |
| `turn.completed` | theo | Agent turn ends (triggers auto-edges in Phase 13) |
| `turn.failed` | theo | Agent turn errored |
| `session.created` | system | New SDK session started |
| `session.released` | system | Session ended (timeout/core change) |
| `session.compacting` | system | Transcript compaction starting |
| `session.compacted` | system | Transcript compaction finished |

### Memory (phases 5–9, 13, 13a — 17 events)

| Event Type | Phase | Actor | Purpose |
| ---------- | ----- | ----- | ------- |
| `memory.node.created` | 5 | theo/system | Knowledge node created |
| `memory.node.updated` | 5 | theo/system | Knowledge node modified |
| `memory.node.confidence_adjusted` | 5 | system | Node confidence changed |
| `memory.node.merged` | 13 | system | Two near-duplicate nodes merged |
| `memory.edge.created` | 5 | theo/system | Edge created between nodes |
| `memory.edge.expired` | 5 | system | Edge temporally closed |
| `memory.episode.created` | 6 | user/theo | Episode added to session |
| `memory.core.updated` | 6 | theo | Core memory slot changed |
| `contradiction.requested` | 13 | system | Classification requested (decision) |
| `contradiction.classified` | 13 | system | Classifier result captured (effect) |
| `memory.user_model.updated` | 8 | theo | User model dimension changed |
| `memory.self_model.updated` | 8 | system | Self model accuracy adjusted |
| `memory.node.accessed` | 7 | system | Node access_count incremented on retrieval |
| `memory.node.decayed` | 13 | system | Node importance reduced by forgetting curve |
| `memory.node.importance.propagated` | 13 | system | Importance boosted on graph neighbors |
| `memory.pattern.synthesized` | 13 | system | Pattern/principle node synthesized from cluster |
| `episode.summarize_requested` / `episode.summarized` | 13 | system | Decision/effect pair for summarization |

### Scheduler (phase 12 — 6 events)

| Event Type | Actor | Purpose |
| ---------- | ----- | ------- |
| `job.created` | scheduler | Scheduled job registered |
| `job.triggered` | scheduler | Scheduled job begins |
| `job.completed` | scheduler | Scheduled job ends |
| `job.failed` | scheduler | Scheduled job errored |
| `job.cancelled` | scheduler | Scheduled job cancelled |
| `notification.created` | scheduler | Notification emitted from job |

### Skill (phase 14 — 2 events)

| Event Type | Actor | Purpose |
| ---------- | ----- | ------- |
| `memory.skill.created` | theo/system | Procedural skill created or refined |
| `memory.skill.promoted` | system | Skill promoted to persona |

### Goal (phase 12a — 21 events)

See `docs/plans/foundation/12a-goal-loops.md` §3 for full payload shapes.

`goal.created`, `goal.confirmed`, `goal.priority_changed`, `goal.plan_updated`,
`goal.lease_acquired`, `goal.lease_released`, `goal.task_started`, `goal.task_progress`,
`goal.task_yielded`, `goal.task_completed`, `goal.task_failed`, `goal.task_abandoned`,
`goal.blocked`, `goal.unblocked`, `goal.reconsidered`, `goal.paused`, `goal.resumed`,
`goal.cancelled`, `goal.completed`, `goal.quarantined`, `goal.redacted`, `goal.expired`.

### Webhook / Reflex / Ideation / Proposal (phase 13b — 27 events)

See `docs/plans/foundation/13b-ideation-and-reflexes.md` §13 for full payload shapes.

Webhook: `webhook.received`, `webhook.verified`, `webhook.rejected`,
`webhook.rate_limited`, `webhook.secret_rotated`, `webhook.secret_grace_expired`.

Reflex: `reflex.triggered`, `reflex.thought`, `reflex.suppressed`.

Ideation: `ideation.scheduled`, `ideation.proposed`, `ideation.duplicate_suppressed`,
`ideation.budget_exceeded`, `ideation.backoff_extended`.

Proposal: `proposal.requested`, `proposal.drafted`, `proposal.approved`,
`proposal.rejected`, `proposal.executed`, `proposal.expired`, `proposal.redacted`.

Egress: `policy.autonomous_cloud_egress.enabled` /
`policy.autonomous_cloud_egress.disabled`, `policy.egress_sensitivity.updated`,
`cloud_egress.turn`.

Degradation: `degradation.level_changed`.

### System (phases 3, 10, 14, 15 — 5 events)

| Event Type | Phase | Actor | Purpose |
| ---------- | ----- | ----- | ------- |
| `system.started` | 14 | system | Engine started |
| `system.stopped` | 14 | system | Engine stopped |
| `system.handler.dead_lettered` | 3 | system | Handler exhausted retries |
| `hook.failed` | 10 | system | Hook threw an error |
| `system.rollback` | 15 | system | Source code rolled back to healthy_commit |

**Event total across all phases: ~86.** All events are version 1 during foundation. The
upcaster registry infrastructure (Phase 2) stays for post-launch use but is not exercised
during foundation development.

## Conventions

Every phase file follows this structure:

- **Motivation** — How this phase brings us closer to the goal
- **Depends on** — Which phases must be complete first
- **Scope** — Files to create/modify
- **Design decisions** — Key choices and rationale
- **Definition of done** — Concrete checklist
- **Test cases** — What to test and how (including failure modes, not just happy paths)
- **Risks** — What could go wrong and mitigations

Phases 12a and 13b were rewritten from scratch after a four-reviewer audit (event-auditor,
resilience-engineer, agentic-ai-scholar, security-reviewer) surfaced 70+ concrete issues.
The rewrites live in this repository as the authoritative specifications.

## What "done" means globally

Theo is done when:

1. A user starts Theo and is greeted by an agent with a distinct voice.
2. Theo leads an onboarding conversation to learn about its owner.
3. Every turn assembles personalized context from multi-tier memory with the external
   content envelope rule enforced.
4. Memory is stored and retrieved via RRF fusion with forgetting curves and provenance
   filters.
5. Theo learns procedural skills that improve with use.
6. The scheduler runs built-in jobs autonomously across four priority classes.
7. Contradictions are detected as split decision/effect events.
8. The executive loop maintains a persistent stack of goals, picks the highest-value one,
   runs one turn with budget caps, and yields.
9. Webhooks can trigger reflex thinking turns that are fully isolated behind the external
   content envelope and the external tool allowlist.
10. The ideation job dreams about intersections in the graph and proposes new goals that
    cannot escalate past autonomy level 2 without owner approval.
11. Proposals stage artifacts in the workspace with TTL + GC; nothing leaves Theo's
    boundary without explicit owner approval unless the autonomy level allows it.
12. Theo self-updates its own source code with rollback safety.
13. Theo runs always-on via `launchd` with full OTel-compatible observability (logs,
    traces, metrics, per-iteration cost accounting including advisor tokens).
14. Every action is auditable and replayable via the event log; the projection can be
    rebuilt from zero without divergence.
