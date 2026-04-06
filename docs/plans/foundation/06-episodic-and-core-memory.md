# Phase 6: Episodic & Core Memory

## Motivation

Episodic memory is Theo's autobiographical record — every conversation message, stored and
searchable. Core memory is the agent's working RAM — four named JSON documents (`persona`, `goals`,
`user_model`, `context`) loaded into the system prompt on every turn. Together they provide the
foundation for continuity: when a session ends, episodic memory preserves the conversation, and core
memory carries the agent's identity and goals forward.

Without episodic memory, conversations are ephemeral. Without core memory, the agent has no
persistent identity. This phase makes both operational.

## Depends on

- **Phase 3** — Event bus (mutations emit events; transaction support for atomic state+event writes)
- **Phase 4** — Memory schema (tables exist)
- **Phase 5** — Embedding service (episodes get embeddings)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/episodic.ts` | `EpisodicRepository` — append, getBySession, search |
| `src/memory/core.ts` | `CoreMemoryRepository` — read, readSlot, update (changelogged) |
| `src/memory/types.ts` | Shared memory types: `Episode`, `CoreMemorySlot`, `CoreMemory`, `JsonValue` |
| `tests/memory/episodic.test.ts` | Episode CRUD, embedding generation, session queries |
| `tests/memory/core.test.ts` | Core memory read/write, changelog, hash computation, concurrent updates |

## Design Decisions

### Atomic State + Event Pattern

All repositories follow a single pattern: the projection write and the event emission happen in the
same database transaction. If either fails, both roll back. This uses the bus's `{ tx }` option
(defined in Phase 3) to emit the event within the caller's transaction.

This is the standard pattern for all repositories in the codebase:

```typescript
// State change and event emission are atomic — neither persists without the other.
await this.sql.begin(async (tx) => {
  // 1. Read current state (if needed for changelog)
  // 2. Write the projection
  // 3. Emit the event within the same transaction
  await this.bus.emit({ type: "...", ... }, { tx });
});
```

### Shared Types

```typescript
type JsonPrimitive = string | number | boolean | null;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
```

All structured data stored as JSONB uses `JsonValue`, not `unknown`. This provides type safety while
remaining flexible enough for arbitrary JSON documents.

### Episode Types

```typescript
interface Episode {
  readonly id: number;
  readonly sessionId: string;
  readonly role: "user" | "assistant";
  readonly body: string;
  readonly embedding: Float32Array | null;
  readonly supersededBy: number | null;
  readonly createdAt: Date;
}
```

Episodes are append-only. Never UPDATE an episode's `body`. Consolidation (Phase 13) creates a new
summary episode and sets `superseded_by` on the originals.

### EpisodicRepository

```typescript
class EpisodicRepository {
  constructor(
    private readonly sql: Sql,
    private readonly bus: EventBus,
    private readonly embeddings: EmbeddingService,
  ) {}

  async append(input: {
    sessionId: string;
    role: "user" | "assistant";
    body: string;
    actor: Actor;
  }): Promise<Episode> {
    // Embedding computed outside the transaction — it's CPU-bound and idempotent.
    // If it fails, we haven't written anything yet.
    const embedding = await this.embeddings.embed(input.body);

    // INSERT and event emission are atomic.
    const episode = await this.sql.begin(async (tx) => {
      const [row] = await tx`
        INSERT INTO episode (session_id, role, body, embedding)
        VALUES (${input.sessionId}, ${input.role}, ${input.body}, ${embedding})
        RETURNING *
      `;

      await this.bus.emit({
        type: "memory.episode.created",
        version: 1,
        actor: input.actor,
        data: { episodeId: row.id, sessionId: input.sessionId, role: input.role },
        metadata: { sessionId: input.sessionId },
      }, { tx });

      return rowToEpisode(row);
    });

    return episode;
  }

  async getBySession(sessionId: string): Promise<readonly Episode[]> {
    const rows = await this.sql`
      SELECT * FROM episode
      WHERE session_id = ${sessionId} AND superseded_by IS NULL
      ORDER BY created_at ASC
    `;
    return rows.map(rowToEpisode);
  }

  async linkToNode(episodeId: number, nodeId: number): Promise<void> {
    await this.sql`
      INSERT INTO episode_node (episode_id, node_id)
      VALUES (${episodeId}, ${nodeId})
      ON CONFLICT DO NOTHING
    `;
  }
}
```

### Core Memory Types

```typescript
type CoreMemorySlot = "persona" | "goals" | "user_model" | "context";

interface CoreMemory {
  readonly persona: JsonValue;
  readonly goals: JsonValue;
  readonly user_model: JsonValue;
  readonly context: JsonValue;
}
```

### Error Types

```typescript
class SlotNotFoundError extends Error {
  readonly slot: CoreMemorySlot;
  constructor(slot: CoreMemorySlot) {
    super(`Core memory slot not found: ${slot}`);
    this.slot = slot;
  }
}
```

### CoreMemoryRepository

```typescript
class CoreMemoryRepository {
  constructor(
    private readonly sql: Sql,
    private readonly bus: EventBus,
  ) {}

  async read(): Promise<CoreMemory> {
    const rows = await this.sql`SELECT slot, body FROM core_memory ORDER BY slot`;
    // Build CoreMemory object from rows
  }

  async readSlot(slot: CoreMemorySlot): Promise<Result<JsonValue, SlotNotFoundError>> {
    const [row] = await this.sql`SELECT body FROM core_memory WHERE slot = ${slot}`;
    if (!row) {
      // Slots are seeded in the migration, so this should never happen.
      // But defensive coding for decade-long operation — a manual DELETE,
      // a bad migration, or data corruption could remove a slot.
      return { ok: false, error: new SlotNotFoundError(slot) };
    }
    return { ok: true, value: row.body as JsonValue };
  }

  async update(slot: CoreMemorySlot, newBody: JsonValue, actor: Actor): Promise<void> {
    await this.sql.begin(async (tx) => {
      const [current] = await tx`SELECT body FROM core_memory WHERE slot = ${slot}`;

      await tx`UPDATE core_memory SET body = ${this.sql.json(newBody)} WHERE slot = ${slot}`;

      await tx`
        INSERT INTO core_memory_changelog (slot, body_before, body_after, changed_by)
        VALUES (${slot}, ${current.body}, ${this.sql.json(newBody)}, ${actor})
      `;

      await this.bus.emit({
        type: "memory.core.updated",
        version: 1,
        actor,
        data: { slot, changedBy: actor },
        metadata: {},
      }, { tx });
    });
  }

  /** Hash of all core memory slots — used to detect when system prompt needs refresh */
  async hash(): Promise<string> {
    const [row] = await this.sql`
      SELECT md5(string_agg(slot || body::text, ',' ORDER BY slot)) AS hash
      FROM core_memory
    `;
    return row.hash;
  }
}
```

### Core Memory Hash for Session Invalidation

When core memory changes (especially `persona` or `goals`), the system prompt is fundamentally
different. The `hash()` method computes a deterministic hash of all slots. The session manager
(Phase 10) compares hashes to detect when a session should be released and a new one started with
the updated system prompt.

## Definition of Done

- [ ] `EpisodicRepository.append()` inserts an episode with embedding and emits
  `memory.episode.created` atomically in a single transaction
- [ ] `EpisodicRepository.getBySession()` returns episodes in chronological order, excluding
  superseded
- [ ] `EpisodicRepository.linkToNode()` creates an episode-node cross-reference
- [ ] `CoreMemoryRepository.read()` returns all 4 slots
- [ ] `CoreMemoryRepository.readSlot()` returns `Result<JsonValue, SlotNotFoundError>`
- [ ] `CoreMemoryRepository.update()` modifies the slot, records changelog, and emits event in a
  single transaction
- [ ] Changelog captures before/after values and actor
- [ ] `CoreMemoryRepository.hash()` returns a consistent hash that changes when any slot is updated
- [ ] All `JsonValue` types used — no `unknown` for JSONB data
- [ ] `just check` passes

## Test Cases

### `tests/memory/episodic.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Append episode | Valid input | Episode with ID, embedding, event emitted — all in one transaction |
| Append atomicity | Embedding succeeds but DB fails | No event emitted, no episode persisted |
| Get by session | 3 episodes in same session | Returns all 3 in order |
| Get excludes superseded | Episode with superseded_by set | Not returned |
| Link to node | Valid episode + node IDs | Row in episode_node |
| Link idempotent | Link same pair twice | No error (ON CONFLICT DO NOTHING) |

### `tests/memory/core.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Read all | Fresh DB (seeded) | 4 slots with empty JSON |
| Read slot | Specific slot | Returns `{ ok: true, value: ... }` |
| Read missing slot | Slot deleted from DB | Returns `{ ok: false, error: SlotNotFoundError }` |
| Update slot | New body for "persona" | Body updated, changelog recorded, event emitted — all atomic |
| Update atomicity | Event emission fails in transaction | Neither state change nor changelog persists |
| Changelog captures diff | Update "goals" | Before = old, after = new, changed_by = actor |
| Hash changes | Update any slot | Hash differs from before |
| Hash stable | No changes | Same hash on repeated calls |
| Concurrent updates | Two updates to same slot | Second update sees first's changelog; both changelogs exist with correct before/after |

## Risks

**Low risk.** Both services are thin wrappers around SQL + event emission. The episodic memory is
append-only (simplest possible write pattern). Core memory is 4 rows with JSONB (simple CRUD).

The atomic transaction pattern (state + event in the same `sql.begin`) is the key correctness
property. Without it, a crash between the state write and the event emission would leave the system
inconsistent. The `{ tx }` option on `bus.emit()` ensures the event INSERT shares the caller's
transaction.

The only subtlety is the `superseded_by` mechanism — it's defined in this phase but not used until
Phase 13 (consolidation). The query filter (`WHERE superseded_by IS NULL`) is added now to avoid
future migration.
