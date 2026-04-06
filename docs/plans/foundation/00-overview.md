# Theo Implementation Plan -- Overview

16 phases. Each is incremental, testable, and completable in one Opus 4.6 (1M) context window.

## Dependency Graph

```text
Phase 1: Foundation (config, pool, migrations, errors)
    |
Phase 2: Event Types (unions, IDs, upcasters)
    |
Phase 3: Event Log & Bus (table, store, dispatch, checkpoints, replay)
    |
    +-----+-----+
    |           |
Phase 4     Phase 5-6 depend on bus
Memory      (can start after Phase 4 migration)
Schema
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
Phase 9: MCP Memory Tools (tool server, all memory tools)
    |
Phase 10: Agent Runtime (context assembly, SDK query, hooks, sessions)
    |
Phase 11: CLI Gate (interactive CLI, streaming)
    |
Phase 12: Scheduler (jobs, cron, execution, built-in jobs)
    |
Phase 13: Background Intelligence (contradiction detection, auto-edges, consolidation)
    |
Phase 14: Subagents, Onboarding & Engine Lifecycle
    |
Phase 15: Operationalization (launchd, self-update, observability, logging)
```

## Phase Summary

| # | Phase | Key Deliverables | Est. Lines | Risk |
| --- | ------- | ------------------ | ----------- | ------ |
| 1 | Foundation | Config (zod), pool (postgres.js), migration runner, `Result<T,E>` errors | ~600 | Low |
| 2 | Event Types | `TheoEvent` union, ULID `EventId`, upcaster registry, `CURRENT_VERSIONS` | ~800 | Low |
| 3 | Event Log & Bus | Partitioned events table, `EventBus.emit()` (write + dispatch), handler checkpoints, replay, tx support | ~1200 | Medium-High |
| 4 | Memory Schema | Single migration: node, edge, episode, core_memory, user_model, self_model tables, skill table, access_count/last_accessed_at on node, pattern/principle NodeKinds | ~600 | Low |
| 5 | Embeddings & Knowledge Graph | Local ONNX embeddings, `NodeRepository` (CRUD, adjustConfidence, findSimilar), `EdgeRepository`, temporal versioning | ~1200 | Medium |
| 6 | Episodic & Core Memory | `EpisodicRepository`, 4-slot `CoreMemoryRepository`, changelog, atomic state+event writes | ~700 | Low |
| 7 | Hybrid Retrieval | Single-query RRF fusing vector + FTS + graph, configurable weights, importance-weighted RRF, access tracking on retrieval | ~600 | High |
| 7.5 | Bootstrap Identity | Persona seed, goals seed, `buildPrompt()` template, onboarding detection | ~400 | Low |
| 8 | Models & Privacy | `UserModelRepository`, `SelfModelRepository`, `PrivacyFilter` at storage boundary | ~900 | Medium |
| 9 | MCP Memory Tools | MCP tool server: store_memory, search_memory, read_core, update_core, update_user_model | ~800 | Low |
| 10 | Agent Runtime | Context assembly from memory tiers, SDK `query()` integration (verified v0.2.92), hooks, session management, cache-optimized prompt ordering, smart session management (topic continuity, depth tracking) | ~1800 | High |
| 11 | CLI Gate | Interactive terminal gate, streaming responses, ephemeral events | ~600 | Low |
| 12 | Scheduler | Job store, cron runner, built-in jobs (consolidation, reflection, scanning) | ~1000 | Medium |
| 13 | Background Intelligence | Contradiction detection (rate-limited, haiku), auto-edges, consolidation with node merging, importance propagation, forgetting curves, abstraction hierarchy synthesis | ~1100 | Medium |
| 14 | Subagents, Onboarding & Lifecycle | 8 subagent `AgentDefinition`s, psychologist-led onboarding, `Engine` state machine, reflector creates/refines skills, SkillRepository | ~1000 | Low-Medium |
| 15 | Operationalization | `launchd` plist, structured logging (JSON + file rotation), OTel tracing + metrics, self-update with rollback, workspace layout | ~1200 | Medium |

**Total: ~13,800 lines across 16 phases.**

## Systemic Decisions

These cross-cutting decisions affect multiple phases and are documented here to avoid repetition:

### 1. Event+Projection Atomicity via Bus Transaction Parameter

The event bus `emit()` accepts an optional `{ tx }` parameter. When provided, the event is written
inside the caller's SQL transaction, guaranteeing that the event and the projection update either
both commit or both roll back. Used by: Phase 3 (bus design), Phase 5 (node/edge mutations), Phase 6
(core memory updates), Phase 13 (node merging).

### 2. Streaming Architecture

The data flow for a user message: SDK `query()` runs the agent loop --> the engine consumes the
async generator --> ephemeral events are emitted for intermediate state (tool calls, thinking) -->
the gate receives events and streams them to the user. Ephemeral events are typed separately in the
event union and are never persisted to the event log.

### 3. SDK API Verified Against v0.2.92

All SDK integration code (Phase 10 `query()` calls, Phase 13 `query()` for contradiction
classification, Phase 13 `query()` for episode summarization) uses the verified API surface:

- `query()` returns `AsyncGenerator` -- must be consumed with `for await`
- `SDKResultSuccess` has `structured_output?: unknown` for JSON schema output
- `AgentDefinition` type used for subagent configuration (Phase 14)
- `options.settingSources: []` for prompt isolation
- `options.permissionMode: 'bypassPermissions'` for non-interactive calls

### 4. GENERATED BY DEFAULT for IDs

All auto-increment primary keys use `GENERATED BY DEFAULT AS IDENTITY`. This allows event replay to
insert rows with explicit IDs (overriding the default) while still auto-generating IDs during normal
operation. `GENERATED ALWAYS` would reject explicit IDs during replay.

### 5. Context Caching via Stable Prompt Ordering

The system prompt is ordered from most-stable to most-volatile content to maximize Anthropic API
cache hits: Static Instructions + Persona (rarely changes) → Active Skills + Goals + User Model
(session-level) → Current Context + Relevant Memories (per-turn). Two explicit cache breakpoints
divide the prompt into three zones. Used by: Phase 7.5 (buildPrompt ordering), Phase 10 (context
assembly).

## Event Catalog

Events added across all phases (for reference when implementing Phase 2):

| Event Type | Phase | Actor | Purpose |
| ------------ | ------- | ------- | --------- |
| `message.received` | 10 | user | Incoming user message |
| `turn.started` | 10 | theo | Agent turn begins |
| `turn.completed` | 10 | theo | Agent turn ends (triggers auto-edges in Phase 13) |
| `turn.failed` | 10 | theo | Agent turn errored |
| `session.created` | 10 | system | New SDK session started |
| `session.released` | 10 | system | Session ended (timeout/core change) |
| `session.compacting` | 10 | system | Transcript compaction starting |
| `session.compacted` | 10 | system | Transcript compaction finished |
| `memory.node.created` | 5 | theo/system | Knowledge node created (triggers contradiction detection in Phase 13) |
| `memory.node.updated` | 5 | theo/system | Knowledge node modified |
| `memory.node.confidence_adjusted` | 5 | system | Node confidence changed via adjustConfidence |
| `memory.node.merged` | 13 | system | Two near-duplicate nodes merged |
| `memory.edge.created` | 5 | theo/system | Edge created between nodes |
| `memory.edge.expired` | 5 | system | Edge temporally closed |
| `memory.episode.created` | 6 | user/theo | Episode added to session |
| `memory.core.updated` | 6 | theo | Core memory slot changed |
| `memory.contradiction.detected` | 13 | system | Two nodes found to contradict |
| `memory.user_model.updated` | 8 | theo | User model dimension changed |
| `memory.self_model.updated` | 8 | system | Self model accuracy adjusted |
| `job.created` | 12 | scheduler | Scheduled job registered |
| `job.triggered` | 12 | scheduler | Scheduled job begins |
| `job.completed` | 12 | scheduler | Scheduled job ends |
| `job.failed` | 12 | scheduler | Scheduled job errored |
| `job.cancelled` | 12 | scheduler | Scheduled job cancelled |
| `notification.created` | 12 | scheduler | Notification emitted from job |
| `system.started` | 14 | system | Engine started |
| `system.stopped` | 14 | system | Engine stopped |
| `system.handler.dead_lettered` | 3 | system | Handler exhausted retries |
| `hook.failed` | 10 | system | Hook threw an error |
| `memory.node.accessed` | 7 | system | Node access_count incremented on retrieval |
| `memory.skill.created` | 14 | theo/system | Procedural skill created or refined |
| `memory.skill.promoted` | 14 | system | Skill promoted to persona |
| `memory.node.decayed` | 13 | system | Node importance reduced by forgetting curve |
| `memory.node.importance.propagated` | 13 | system | Importance boosted on graph neighbors |
| `memory.pattern.synthesized` | 13 | system | Pattern or principle node synthesized from cluster |
| `session.topic_continued` | 10 | system | Session continued due to topic continuity |
| `system.rollback` | 15 | system | Source code rolled back to healthy_commit |

## Conventions

Every phase file follows this structure:

- **Motivation** -- How this phase brings us closer to the goal
- **Depends on** -- Which phases must be complete first
- **Scope** -- Files to create/modify
- **Design decisions** -- Key choices and rationale
- **Definition of done** -- Concrete checklist
- **Test cases** -- What to test and how
- **Risks** -- What could go wrong and mitigations

## What "done" means globally

Theo is done when: a user starts Theo and is greeted by an agent with a distinct voice, Theo leads
an onboarding conversation to learn about its owner, assembles personalized context from multi-tier
memory on every turn, stores and retrieves knowledge via RRF fusion, learns procedural skills that
improve with use, runs scheduled background jobs autonomously, detects contradictions, builds a user
model, forgets stale memories gracefully, self-updates its own source code with rollback safety,
runs always-on via launchd with full OTel-compatible observability (logs, traces, metrics), and does
all of this with full event sourcing so every action is auditable and replayable.
