# Theo — Architecture

Personal agent with persistent memory, autonomous scheduling, and full event sourcing.
Bun. TypeScript. Claude Agent SDK. PostgreSQL + pgvector.

This document describes the target architecture. The current repository only
implements a small scaffold around this design.

---

## Philosophy

Theo is not a chatbot with memory bolted on. It is a **living system** — one that accumulates knowledge, builds a model of its owner, acts autonomously, and remembers everything.

The Claude Agent SDK is the runtime. Theo does not wrap it — Theo extends it. The SDK provides the agent loop, tool execution, session management, context compaction, subagent orchestration, and budget controls. Theo provides the persistent identity: memory, event history, accumulated understanding, and autonomous scheduling. Every LLM interaction goes through the SDK. No direct API calls.

The SDK's hooks are the bridge between the agent loop and Theo's event system. Hooks fire at every lifecycle point — before and after tool calls, on compaction, on turn completion. These hooks emit events into Theo's persistent bus, which writes them to the event log and updates projections. This means the agent loop itself drives Theo's state changes, not an external wrapper observing from outside.

An SDK session is working memory — short-lived, focused, released when the task is done. Theo's event log is long-term memory — durable, unlimited, append-only. When a session ends, nothing is lost. The event log has the complete record, and RRF search rebuilds relevant context for the next session. The user experiences continuity because Theo's memory system carries the thread, not a bloated conversation history.

Every decision optimizes for two horizons: developer ergonomics today, and a decade of continuous operation.

The biggest architectural bet is event sourcing. It adds complexity (upcasters, projections, partition management) but unlocks capabilities that are impossible without it: time travel, complete audit trails, new analytical views from existing data, and the guarantee that no information is ever lost. For an agent designed to run for a decade, this is the right tradeoff.

Errors are events, not exceptions. A failed turn emits `turn.failed`. A failed handler is dead-lettered and recorded. A rejected message (privacy filter, budget cap) is an event with a reason. The system never silently drops state — every failure is part of the audit trail.

---

## Core Primitives

Five primitives. Everything else is a feature built on top.

```text
              ┌─────────────────────────┐
              │      Memory Tiers       │◄───── Projections
              │  (graph, episodic,      │            ▲
              │   core, user, self)     │            │
              └───────────┬─────────────┘       bus handlers
                system prompt                        │
                          │                          │
                          ▼                          │
┌────────────────────────────────────────────┐  ┌────┴──────────┐
│            Agent SDK Runtime               │  │  Event Bus    │
│                                            │──│  (persistent  │
│  agents · hooks · sessions · MCP tools     │  │   dispatch)   │
│  structured output · thinking · budget     │  └────┬──────────┘
└──────────┬─────────────────────────────────┘  writes│
        gates                               ┌────────▼─────────┐
           │                                │    Event Log     │
      ┌────▼────┐                           │  (append-only    │
      │Telegram │                           │   immutable)     │
      │   CLI   │                           └──────────────────┘
      └─────────┘
```

The cycle: Memory feeds the system prompt → Agent SDK runs the loop and uses MCP memory tools → hooks emit events to the bus → bus writes to the event log and updates projections (including memory) → memory feeds the next turn.

| Primitive | One-line | Durable? |
| --------- | -------- | -------- |
| **Event Log** | Append-only record of everything that ever happens | Yes — PostgreSQL, partitioned by month |
| **Event Bus** | Persistent dispatch, fed by SDK hooks and MCP tools | Yes — unified with the event log, handler checkpoints in PostgreSQL |
| **Memory** | Multi-tier knowledge store — the agent's long-term mind | Yes — dedicated tables, projected from events |
| **Agent** | SDK runtime — agent loop, hooks, sessions, subagents | SDK sessions are working memory; lifecycle events persisted |
| **Scheduler** | Autonomous subagent invocations on cron or trigger | Yes — PostgreSQL |

---

## 1. Event Log

### Why

**The event log is the primary record.** Tables like `node`, `episode`, `core_memory` are projections — derived views that can be rebuilt from the log at any time. This means every question — "what did Theo know on March 15th?", "why did Theo say that?" — is answerable by replaying the log to that point.

### Design

Every event is a typed, immutable record:

```typescript
interface TheoEvent<T extends string = string, D = Record<string, unknown>> {
  readonly id: string;          // ULID — sortable, unique, timestamp-embedded
  readonly type: T;
  readonly version: number;     // schema version for this event type
  readonly timestamp: Date;
  readonly actor: Actor;
  readonly data: D;
  readonly metadata: EventMetadata;
}

type Actor = "user" | "theo" | "scheduler" | "system";

interface EventMetadata {
  traceId?: string;             // correlation ID
  sessionId?: string;           // conversation session
  causeId?: string;             // ULID of the event that caused this one
  gate?: string;                // originating interface
}
```

TypeScript's discriminated unions make this powerful — you get exhaustive type checking across all event handlers:

```typescript
type ChatEvent =
  | TheoEvent<"message.received", { body: string; channel: string }>
  | TheoEvent<"turn.started", { sessionId: string; speedTier: string }>
  | TheoEvent<"turn.completed", { responseBody: string; durationMs: number }>
  | TheoEvent<"turn.failed", { errorType: string; message: string }>;

type MemoryEvent =
  | TheoEvent<"memory.node.created", { nodeId: number; kind: string; body: string }>
  | TheoEvent<"memory.edge.created", { edgeId: number; sourceId: number; targetId: number; label: string }>
  | TheoEvent<"memory.contradiction.detected", { nodeId: number; conflictId: number; explanation: string }>
  // ...

type SchedulerEvent =
  | TheoEvent<"job.triggered", { jobId: string; executionId: string }>
  | TheoEvent<"job.completed", { jobId: string; resultSummary: string }>
  // ...

type SystemEvent =
  | TheoEvent<"system.started", { version: string }>
  | TheoEvent<"system.stopped", { reason: string }>
  // ...

// The full union
type Event = ChatEvent | MemoryEvent | SchedulerEvent | SystemEvent;
```

### Event Versioning

Events are immutable forever. Schemas evolve through **upcasters** — functions that transform old event shapes to new ones at read time:

```typescript
// When "memory.node.created" gains a new required field in v2:
upcasters.register("memory.node.created", 1, (data) => ({
  ...data,
  sensitivity: data.sensitivity ?? "normal",
}));
```

Old events stay untouched in the log. Upcasters run lazily during projection replay. This means schema evolution never requires a data migration.

### Storage

Single append-only table, partitioned by month for decade-scale retention. ULID as primary key gives natural time-ordering without a separate index.

Partitions are created ahead of time (or on-demand). Old partitions can be archived to cold storage without affecting the live system.

**Snapshots.** Full replay from epoch doesn't scale over years. The consolidation job (every 6h) captures snapshots — serialized projection state paired with the ULID of the last event included. Replay starts from the nearest snapshot, not from the beginning. Snapshots are stored in a dedicated table, one row per projection. If a snapshot is corrupted or stale, the system falls back to the previous one or replays from zero. Snapshots are cheap insurance — a few KB of JSON per projection, written once every few hours.

**Migrations.** Forward-only SQL migrations, numbered sequentially. No down migrations — for a system designed to run for a decade, rollback scripts are a liability. If a migration is wrong, write a new one that fixes it.

### What Is NOT an Event

Not everything goes through the log. Ephemeral, high-frequency data skips it:

- **LLM streaming chunks** — reconstructible from the final response, not worth the write amplification
- **Health check pings** — observability concern, not state
- **Embedding computations** — deterministic from input, cacheable separately

Rule of thumb: if losing it across a restart would change the agent's behavior or knowledge, it's an event. If it's just a performance optimization, it's not.

---

## 2. Event Bus

### Rationale

The event log is the persistent record, but it's also the backbone of real-time coordination. The bus unifies durability and dispatch — every durable event is written to PostgreSQL and then dispatched to in-memory handlers in a single operation. No separate message broker. No polling.

Events flow into the bus from two sources:

1. **SDK hooks** — lifecycle events. Turn started, turn completed, compaction, session changes.
2. **MCP tool handlers** — domain events. Memory stored, edge created, core memory updated.

Both call `bus.emit()`. The bus handles persistence and dispatch uniformly.

### Architecture

A custom implementation, tailored for Theo. The bus and the log are the same system.

**Write path:**

```text
bus.emit(event)
  → assign ULID + timestamp
  → INSERT into events table
  → dispatch to registered in-memory handlers
```

**Handler registration:**

```typescript
interface EventBus {
  on<T extends Event["type"]>(
    type: T,
    handler: Handler<T>,
    options?: { id: string }  // stable handler ID for checkpointing
  ): void;

  emit(event: Omit<Event, "id" | "timestamp">): Promise<Event>;

  start(): Promise<void>;   // replay from checkpoints, then listen
  stop(): Promise<void>;    // drain in-flight handlers
}
```

**Checkpointing.** Each durable handler has a stable ID and a cursor in a `handler_cursors` table — the ULID of the last event it successfully processed. After a handler completes, its cursor advances. This gives at-least-once delivery without a message broker.

**Handlers never block each other.** One failing handler is caught, logged, and counted. Others continue. A handler that repeatedly fails on the same event is dead-lettered after a configurable retry count — the cursor advances and the failure is recorded.

### Continuation

On startup, `start()` reads each handler's cursor and replays events from that point forward. If the process crashed between writing an event and dispatching it, the replay catches up. If the process was down for hours, every event written during that time is replayed in order.

Handlers are idempotent by convention — processing the same event twice must be safe. The ULID in each event makes deduplication trivial where needed.

This means the system self-heals on restart. No manual intervention, no lost events, no orphaned state.

### Ephemeral Events

Some events skip the log entirely and go directly through the bus — streaming chunks, internal signals. These use a separate `EphemeralEvent` type, not the durable `Event` union. The type system enforces the distinction at compile time — you cannot accidentally skip persistence for a durable event.

---

## 3. Memory

The memory system is a multi-tier knowledge store. It is the reason Theo is more than a chatbot — it accumulates understanding over years of interaction.

Design principles:

- **Multi-tier storage** with different characteristics per tier
- **Hybrid retrieval (RRF)** fusing vector, full-text, and graph signals
- **Temporal edge versioning** — relationships have history, not just current state
- **Auto-edge discovery** through co-occurrence
- **Contradiction detection** as a background process
- **Privacy as a gate, not a filter** — reject at the boundary, not after storage
- **Agent-controlled memory** via MCP tools — the LLM decides what to remember

### 3.1 Tiers

```text
┌──────────────────────────────────────────────────────────────┐
│                    MCP Tool Server                            │
│              (how the agent accesses memory)                  │
└───────┬────────┬────────┬──────────┬────────────┬────────────┘
        │        │        │          │            │
   ┌────▼───┐ ┌──▼───┐ ┌─▼────┐ ┌───▼─────┐ ┌───▼──────┐
   │ Graph  │ │Episod│ │ Core │ │  User   │ │  Self    │
   │(nodes+ │ │  ic  │ │      │ │  Model  │ │  Model   │
   │ edges) │ │      │ │      │ │         │ │          │
   └────────┘ └──────┘ └──────┘ └─────────┘ └──────────┘
        │
   ┌────▼─────────────────────────────┐
   │     Hybrid Retrieval (RRF)       │
   │  vector + full-text + graph      │
   └──────────────────────────────────┘
```

**Knowledge Graph (Nodes + Edges)** — Semantic facts, preferences, observations. Each node has a kind, body text, embedding vector, trust/confidence/importance scores, and privacy sensitivity. Edges are temporally versioned relationships — updating an edge expires the old one and creates a new one. Full history preserved.

**Episodic Memory** — Conversation messages. The agent's autobiographical record. Linked to knowledge nodes via a cross-reference table (`episode_node`) that powers auto-edge discovery.

**Core Memory** — Four named JSON documents always loaded into the system prompt: `persona`, `goals`, `user_model`, `context`. The agent's working RAM. Read every turn, written rarely. Every mutation is changelogged. The `persona` slot is not a static template — it's a living document that Theo evolves over time. Theo develops its own personality, voice, opinions, and style through experience. The `reflector` subagent observes patterns in how Theo communicates and what works, updating the persona as Theo finds its own way. The only hard constraint: Theo speaks directly to its owner, never in third person, never exposing internal process.

**User Model** — Structured multi-dimensional profile of the owner, maintained primarily by the `psychologist` subagent. Dimensions rooted in Jungian analytical psychology — personality typology, shadow patterns, dominant archetypes, individuation markers — alongside practical dimensions: values, communication style, energy patterns, boundaries, cognitive preferences. Confidence is computed from evidence count — the model self-reports its own reliability. The psychologist agent observes behavioral patterns across conversations over time, refining the model as evidence accumulates rather than reacting to isolated data points.

**Self Model** — Calibration tracking per task domain (scheduling, drafting, recommendations, etc.). Records predictions vs outcomes. Feeds future autonomy decisions — the agent graduates to more independence in domains where it's proven accurate.

### 3.2 Hybrid Retrieval — RRF

The retrieval system is the crown jewel. Finding relevant memories is harder than storing them.

**Three search signals, fused in a single SQL query:**

| Signal | What it catches | Example |
| ------ | --------------- | ------- |
| **Vector similarity** | Semantic meaning | "happy" matches "joyful" |
| **Full-text search** | Exact keywords the embedding misses | "PostgreSQL 18" matches precisely |
| **Graph traversal** | Associatively connected concepts | User mentions "work" → traverses to "project X" → reaches "deadline Friday" |

**Reciprocal Rank Fusion** combines the three ranked lists:

```text
score(node) = 1/(k + rank_vector) + 1/(k + rank_fts) + 1/(k + rank_graph)
```

A node appearing in all three signals gets the highest score. A node in only one signal still surfaces, just lower-ranked. The `k` constant (default 60) controls how much being #1 vs #10 matters within each signal.

**Graph traversal detail:** The top N vector hits become "seeds." A recursive query walks their edges up to M hops deep, accumulating weight multiplicatively. This means strongly-connected relevant nodes surface even if they're not semantically similar to the query text.

**The entire fusion happens in one database round-trip** — a 7-CTE SQL query. No application-level merging, no multiple round trips. PostgreSQL does the join, the ranking, and the fusion.

**Graceful degradation:** The query uses FULL OUTER JOIN across signals. No FTS matches? Score from vector + graph. Empty graph? Vector + FTS. The system always returns the best available results with whatever signals are present.

### 3.3 Auto-Edges

Automatic relationship discovery. When nodes are created or discussed in the same conversation turn, they get linked.

**Phase 1 — Recording:** When the agent stores a memory during a turn, the episode-node cross-reference is recorded. "This node was mentioned in this episode."

**Phase 2 — Extraction:** After each turn, a bus handler on `turn.completed` finds all node pairs that co-occurred in the same session's episodes. Each pair gets a `co_occurs` edge with weight proportional to co-occurrence count (saturates at 5 co-occurrences).

This means the knowledge graph self-organizes over time. Concepts that are frequently discussed together become strongly connected, improving future retrieval without any explicit linking.

**The agent can also create edges explicitly** via the `link_memories` tool — labeled relationships like "works_on", "caused_by", "contradicts". These complement the automatic co-occurrence edges with semantic richness.

### 3.4 Contradiction Detection

When a new node is stored, a fire-and-forget background task:

1. Finds semantically similar nodes of the same kind (cosine similarity > threshold)
2. Asks the SDK for a classification — a lightweight `query()` with `tools: []`, no MCP servers, `persistSession: false`, `maxTurns: 1`, and structured output:

```typescript
outputFormat: {
  type: "json_schema",
  schema: {
    type: "object",
    properties: {
      contradicts: { type: "boolean" },
      explanation: { type: "string" },
    },
    required: ["contradicts", "explanation"],
  },
}
```

1. On yes: reduces confidence on both nodes, creates a `contradicts` edge with the explanation

This never blocks the write path. The agent can later see the contradiction via retrieval and decide how to resolve it. Knowledge is not silently overwritten — conflicts are surfaced.

### 3.5 Privacy Filter

Enforced as a `PreToolUse` hook on `store_memory`. The hook runs before the tool executes and can block it — the SDK handles the denial gracefully, and the agent sees the tool was rejected.

```text
trust tier → maximum allowed sensitivity
content → detected sensitivity (regex heuristics)
detected > allowed → BLOCK (hook returns deny)
```

Content categories: financial (account numbers, SSN), medical (diagnosis, prescriptions), identity (passport, biometrics), location (addresses, GPS), relationship (personal details).

Trust tiers: owner can store anything, external sources can only store `normal` sensitivity. The filter prevents untrusted inputs from smuggling sensitive data into storage.

This must be a gate, not a post-hoc filter. Once data enters the system, it's in the event log forever (immutable). Reject at the boundary.

### 3.6 Agent Interface — MCP Tools

The agent interacts with memory through MCP tools, implemented with `createSdkMcpServer` and the SDK's `tool()` helper. These sit alongside the SDK's built-in tools — the agent can code, search the web, and manipulate files while also storing and retrieving memories. The agent decides what to remember, what to search for, what to update. No hardcoded "always store the user's message as a fact" logic.

Each MCP tool handler is thin — validate input (zod), emit an event via the bus, return the result. The bus handlers do the actual work (update projections, generate embeddings, trigger auto-edges). This keeps tools consistent with event sourcing: the event is the source of truth, projections derive from it.

| Tool | What it does | Agent autonomy |
| ---- | ------------ | -------------- |
| `store_memory` | Create a knowledge node | Autonomous — agent stores freely |
| `search_memory` | Hybrid RRF search | Autonomous |
| `read_core` | Read all core memory documents | Autonomous |
| `update_core` | Modify a core memory slot (changelogged) | Inform owner |
| `link_memories` | Create a labeled edge between nodes | Autonomous |
| `update_user_model` | Update a user model dimension | Inform owner |
| `schedule_job` | Create a scheduled task (recurrent cron or one-off) | Autonomous |
| `list_jobs` | List active scheduled jobs | Autonomous |
| `cancel_job` | Cancel a scheduled job | Inform owner |

All tools return errors as values, never throw. The agent adapts to failures gracefully.

### 3.7 Context Assembly

Memory feeds the LLM through the system prompt, assembled fresh for every new session. Since sessions are short-lived and released between tasks, this assembly is the primary mechanism for continuity — it's how Theo "remembers" without carrying a long conversation history.

```text
1. Persona          — who the agent is (never truncated)
2. Goals            — what it's working on (never truncated)
3. User Model       — who the owner is (budget-capped)
4. Current Context  — recent activity, active tasks (budget-capped)
5. Relevant Memories — RRF search results for the incoming message (budget-capped)
```

The RRF search in step 5 is the key: it uses the user's incoming message as the query, surfacing memories relevant to what the user is talking about *right now*. Combined with core memory and the user model, the agent starts every session with focused, relevant context instead of a stale transcript.

---

## 4. Agent

The Agent SDK is the runtime. Theo configures it with strict isolation and extends it through hooks and MCP tools.

### Configuration

```typescript
query({
  prompt: userMessage,
  options: {
    mcpServers: { memory: memoryServer },    // Theo's memory tools alongside built-in tools
    systemPrompt: assembleSystemPrompt(),    // fully custom, from memory tiers
    settingSources: [],                      // no external CLAUDE.md or settings
    allowedTools: ["mcp__memory__*"],        // auto-approve memory tools
    thinking: { type: "adaptive" },          // extended reasoning when needed
    resume: activeSessionId,                 // resume if session is valid
  }
})
```

All SDK built-in tools are enabled by default — Read, Write, Edit, Bash, WebSearch, WebFetch, Glob, Grep, and more. Theo is a capable agent, not a restricted chatbot. It can code, search the web, create files, and interact with the system, while also having access to its memory tools via MCP.

**`settingSources: []`** — The isolation that matters. No external CLAUDE.md files, no user settings, no project settings. This prevents random repository configuration from corrupting the agent's identity and behavior. The system prompt is the sole source of instructions.

**`systemPrompt: string`** — Fully custom, assembled fresh from Theo's memory tiers. Replaces the SDK's default prompt entirely. The agent's identity, goals, and context come from Theo's memory system, not from static configuration.

**`thinking: { type: "adaptive" }`** — The SDK enables extended reasoning when the task benefits from it. Complex questions, goal planning, and nuanced memory decisions get deeper thought. Simple acknowledgments don't.

### Hooks

Hooks are the bridge between the SDK's agent loop and Theo's event system. Instead of wrapping the SDK from outside, hooks participate in the loop from inside.

| SDK Hook | Theo Action | Event |
| -------- | ----------- | ----- |
| `UserPromptSubmit` | Record incoming message, persist as episode | `message.received` |
| `PreToolUse` on `store_memory` | Privacy filter — block if sensitivity exceeds trust tier | — (deny) |
| `PreCompact` | Archive full transcript as episodes before summarization | `session.compacting` |
| `PostCompact` | Record that compaction occurred and what was preserved | `session.compacted` |
| `Stop` (success) | Record turn completion with response and usage | `turn.completed` |
| `Stop` (failure) | Record failure with error type and message | `turn.failed` |

MCP tool handlers emit domain events directly via `bus.emit()` — they don't need hooks. The hook table above covers lifecycle events that tools don't produce.

### Sessions

Sessions are short-lived working memory — active while processing a task, released when the task is done. The model performs best with a focused, lean context. Long sessions degrade quality and trigger expensive compaction for no benefit.

**The memory system provides continuity, not sessions.** When a new session starts, the system prompt is assembled fresh from Theo's memory tiers — core memory (persona, goals), user model, RRF search results relevant to the current message. The user experiences seamless continuation because Theo "remembers" through its knowledge graph and episodic memory, not through a long conversation transcript. This is more effective than a stale 200-turn session because the model gets curated, relevant context instead of accumulated noise.

**Session lifecycle:**

1. User sends a message. Theo assembles a system prompt from memory, starts a new SDK session (or resumes an active one).
2. The agent processes the message — may take multiple turns with tool calls, memory operations, reasoning.
3. The user may follow up within the same activity (iterations on code, clarifying questions). The session persists, preserving immediate context.
4. After a short inactivity window (minutes, not hours), the session is released. Key information has already been captured as events and memories during the turns.
5. The next message starts a fresh session with a fresh system prompt. RRF search surfaces whatever context is relevant. The user doesn't notice the boundary.

**Session release triggers:**

- **Inactivity timeout** — short window (configurable, default ~15 minutes). If the user hasn't sent a follow-up, the task is likely done. Release the session, keep the context lean for next time.
- **Core memory changes** — persona or goals updated. The system prompt is fundamentally different. Detected by hashing core memory slots.
- **User request** — explicit "start fresh" command.

**Compaction as a safety net, not a strategy.** Sessions are designed to be short enough that compaction rarely triggers. If it does (long multi-turn task), the `PreCompact` hook archives the full transcript as events before summarization. But the goal is to avoid compaction entirely by keeping sessions focused.

### Subagents

The SDK's subagent system defines specialized agents with isolated context, different models, and restricted tool sets. Each subagent gets a fresh context window — only its final response returns to the parent.

Theo defines named subagents for distinct cognitive modes:

```typescript
agents: {
  coder: {
    description: "Write, edit, and debug code across any language or framework",
    prompt: "You are Theo's software engineering agent...",
    model: "opus",
    maxTurns: 200,
  },
  researcher: {
    description: "Deep investigation — web search, doc reading, synthesis",
    prompt: "You are Theo's research agent...",
    model: "sonnet",
    maxTurns: 50,
  },
  writer: {
    description: "Draft emails, messages, and documents in the owner's voice",
    prompt: "You are Theo's writing agent...",
    model: "sonnet",
    maxTurns: 20,
  },
  planner: {
    description: "Break complex goals into concrete steps and identify dependencies",
    prompt: "You are Theo's planning agent...",
    model: "sonnet",
    maxTurns: 20,
  },
  psychologist: {
    description: "Jungian psychologist — tracks psychological profile, behavioral patterns, individuation",
    prompt: "You are Theo's depth psychology agent, grounded in Jungian analytical psychology...",
    model: "opus",
    maxTurns: 30,
  },
  consolidator: {
    description: "Compress and deduplicate memories",
    prompt: "You are Theo's memory maintenance agent...",
    model: "haiku",
    maxTurns: 20,
  },
  reflector: {
    description: "Analyze behavioral patterns and calibrate self-model",
    prompt: "You are Theo's self-reflection agent...",
    model: "sonnet",
    maxTurns: 10,
  },
  scanner: {
    description: "Surface forgotten commitments and pending follow-ups",
    prompt: "You are Theo's proactive monitoring agent...",
    model: "haiku",
    maxTurns: 10,
  },
}
```

Subagents inherit the parent's MCP memory tools by default. Each can be invoked by the main agent during conversation ("let me think about this more carefully" → delegates to reflector) or by the scheduler for autonomous background work.

### Onboarding

On first interaction, the `psychologist` subagent leads a guided conversation to rapidly build a foundational user model. The approach is grounded in psychological research on how to understand a person quickly and deeply.

**Why narrative, not questionnaires.** People are poor self-reporters on traits but excellent storytellers. Research in narrative psychology (McAdams' Life Story Model) shows that how someone tells their story — what they emphasize, what they skip, how they frame conflict — reveals personality, values, and identity far more reliably than self-ratings. The onboarding is a conversation, not a form.

**Three phases, one sitting:**

1. **Life narrative** — Open-ended prompts that invite stories: key chapters, turning points, proudest moments, biggest challenges. Reveals values, identity, and worldview. Based on McAdams' Life Story Interview adapted for conversational flow.

2. **Structured dimensions** — Targeted questions to fill specific model dimensions efficiently. Communication preferences (direct vs. nuanced, detail vs. big picture). Daily rhythms and energy patterns. Boundaries and non-negotiables. Goals and what "going well" looks like. Draws from the Big Five (OCEAN) framework and Schwartz's Theory of Basic Values, but asked as natural conversation, not inventory items.

3. **Working agreement** — How should Theo behave? What level of autonomy? When to act vs. ask? What topics are off-limits? What does helpful look like? This seeds the `persona` and `goals` core memory slots.

**What the onboarding produces:**

- Initial `user_model` — seeded across all dimensions with confidence scores reflecting the evidence gathered. Low confidence is explicit, not hidden.
- Initial `persona` and `goals` core memory — Theo's starting identity and operating instructions.
- A set of knowledge graph nodes — facts, preferences, values extracted from the conversation.
- Behavioral baseline — the psychologist notes communication patterns observed *during* the onboarding itself (not just what was said, but how).

**Not static.** The onboarding is a foundation, not a finished product. Every subsequent interaction refines the model. The `psychologist` subagent runs periodically via the scheduler, analyzing behavioral patterns across conversations and updating dimensions as evidence accumulates. The user model after a year of interaction will look nothing like the one from onboarding — and that's the point.

### Turn Pipeline

```text
message.received (UserPromptSubmit hook)
  │
  ├── Persist episode            — hook emits event, bus handler stores
  ├── Assemble system prompt     — context from memory tiers + RRF search
  ├── SDK agent loop             — thinking, MCP memory tool calls, reasoning
  │     ├── PreToolUse hooks     — privacy filter on store operations
  │     ├── MCP tool handlers    — emit domain events via bus
  │     └── Stream chunks        → bus (ephemeral) → gates
  ├── turn.completed (Stop hook) — persist response, record usage
  └── Bus handlers fire          — auto-edges, contradiction checks
```

### Gates

Gates are thin adapters that translate external protocols into events and back:

```text
Telegram message → MessageReceived event → Agent SDK turn → Telegram reply
CLI input        → MessageReceived event → Agent SDK turn → CLI output
```

Gates subscribe to response events for streaming delivery (e.g., editing a Telegram message in place as tokens arrive).

Adding a new gate means implementing two things: publishing `MessageReceived` events, and subscribing to response events. The agent doesn't know gates exist.

### Engine States

```text
running ←→ paused → stopped
                       ↓
                     killed (force)
```

Paused queues messages. Stop waits for in-flight turns (with timeout). Kill is the escape hatch.

---

## 5. Scheduler

The scheduler is what makes this an agent, not a chatbot. It acts without being asked.

### Job Design

A scheduled job is a prompt + cron expression + subagent configuration. When the cron fires, the scheduler creates a full agent turn — same memory tools, same SDK, isolated context. Each job runs as an SDK `query()` using the corresponding subagent definition.

```typescript
interface ScheduledJob {
  id: string;             // ULID
  name: string;           // human-readable
  cron: string | null;    // cron expression for recurrent, null for one-off
  agent: string;          // subagent name (e.g., "consolidator")
  prompt: string;         // instruction for this execution
  enabled: boolean;
  maxDurationMs: number;  // execution timeout
  lastRunAt: Date | null;
  nextRunAt: Date;
}
```

### Built-in Jobs

| Job | Frequency | Agent | What it does |
| --- | --------- | ----- | ------------ |
| Consolidation | Every 6h | `consolidator` | Compress old episodes, deduplicate knowledge, capture snapshots |
| Reflection | Weekly | `reflector` | Analyze patterns, update self-model calibration |
| Proactive scan | Daily | `scanner` | Surface forgotten commitments, pending follow-ups |
| Goal execution | Daily | main | Make autonomous progress on active goals |

Each is a real agent turn with access to memory tools. The consolidation job might decide to merge similar nodes or summarize a week of episodes. The proactive scan might notice the user mentioned "send that email by Friday" three days ago and surface it.

Subagent model tiers balance cost and capability — haiku for routine maintenance, sonnet for analysis, opus for deep reasoning.

### Agent-Created Jobs

Theo creates jobs autonomously — both from explicit user requests and on his own initiative via the `schedule_job` MCP tool. Recurrent jobs use cron expressions. One-off jobs run once at a specified time and are cleaned up after execution.

**From user requests:**

> "Remind me to check my GitHub PRs every weekday morning"
> → `schedule_job({ cron: "0 9 * * 1-5", agent: "scanner", prompt: "Check GitHub PRs and surface anything needing attention" })`

**On Theo's own initiative:**

> During conversation, Theo notices the user mentions a deadline next Friday.
> → `schedule_job({ cron: null, runAt: "2026-04-10T09:00", agent: "scanner", prompt: "Remind owner about the Friday deadline they mentioned on April 4th" })`

The psychologist subagent detects a recurring stress pattern on Monday mornings:

> `schedule_job({ cron: "0 8 * * 1", agent: "psychologist", prompt: "Monday check-in: assess energy and offer grounding if needed" })`

The agent treats scheduling as a core capability, not a feature the user has to ask for.

### Execution Model

```text
cron tick
  → check: enabled? not already running?
  → emit job.triggered event
  → execute SDK query() with subagent config + job prompt
  → emit job.completed/failed event
  → compute next_run_at
```

Job executions are tracked with status, token usage, cost, and duration. The event log captures everything — you can audit what autonomous actions the agent took and why.

### Results Surfacing

When a scheduled job produces findings worth reporting (a forgotten commitment, a goal update, a proactive insight), it emits a `notification.created` event. Gates subscribe to notification events and deliver them through their channel — a Telegram message, a CLI alert. The job doesn't need to know which gates exist. Same pattern as chat responses, same decoupling.

### Overdue Jobs

If the system was down and a job's `next_run_at` is in the past, the scheduler runs it once on startup — not once per missed tick. The single execution gets the current state of memory, which is more useful than replaying stale ticks sequentially.

### Concurrency

Jobs run one at a time by default. Each `query()` spawns a child process, so parallel execution is resource-heavy for a single-machine deployment. A `maxConcurrent` setting can override this when the workload justifies it.

---

## 6. Stack

| Concern | Choice | Why it earns its place |
| ------- | ------ | ---------------------- |
| Runtime | Bun | TS-native, fast, built-in test runner + bundler |
| Agent SDK | `@anthropic-ai/claude-agent-sdk` | The runtime — agent loop, hooks, sessions, subagents, MCP, structured output, budget |
| Database | `postgres` (postgres.js) | Fastest PG client, tagged template SQL, cursors, COPY, custom type parsers, zero dependencies |
| Vectors | pgvector | Vector similarity inside PostgreSQL — no separate vector DB |
| Validation | zod | Runtime schemas, env parsing, tool input validation |
| Telegram | grammy | TypeScript-first, modern, well-maintained |
| Embeddings | `@huggingface/transformers` | Local ONNX inference (CoreML on macOS) — upgradeable to Ollama for Metal GPU acceleration |
| Lint + format | biome | Rust-based, all-in-one, sub-millisecond |
| Task runner | just | Language-agnostic, simple, proven |
