# Phase 13a: Memory Resilience

## Motivation

The memory data model is architecturally strong but has structural gaps that will degrade quality
over years of continuous use. An independent review against the cognitive architecture literature
identified eight concrete issues. This phase addresses them before the system goes live, while
schema and behavior changes are still cheap.

The core theme: Theo's memory system treats all memories as equally important, decays them
uniformly, consolidates them bluntly, and provides no recency signal in retrieval. A personal agent
that runs for a decade needs more nuance.

## Depends on

- **Phase 7** -- Hybrid retrieval (RRF query to extend with recency signal)
- **Phase 8** -- Self model (windowed calibration extends self_model_domain)
- **Phase 13** -- Background intelligence (forgetting curves, consolidation logic to modify)

## Scope

### Schema changes

| File | Change |
| ---- | ------ |
| `src/db/migrations/0004_memory_resilience.sql` | New columns, indexes, constraints |

### Files to create

| File | Purpose |
| ---- | ------- |
| `src/memory/salience.ts` | Episode importance scoring (heuristic + agent-set) |
| `tests/memory/salience.test.ts` | Salience scoring unit tests |

### Files to modify

| File | Change |
| ---- | ------ |
| `src/memory/retrieval.ts` | Add recency signal as fourth RRF dimension |
| `src/memory/consolidation.ts` | Importance-gated consolidation, topic-level grouping |
| `src/memory/forgetting.ts` | Kind-specific decay half-lives |
| `src/events/types.ts` | Add `source_event_id` to `NodeCreatedData` |
| `tests/memory/retrieval.test.ts` | Recency signal tests |
| `tests/memory/consolidation.test.ts` | Importance-gated and topic-level tests |
| `tests/memory/forgetting.test.ts` | Kind-specific decay tests |

## Design Decisions

### 1. Episode importance scores

Episodes currently have no salience signal. The consolidation job compresses everything older than
7 days uniformly. High-stakes conversations (emotional, decision-heavy, conflict-laden) deserve
preservation at full fidelity.

```sql
-- Migration 0004
ALTER TABLE episode ADD COLUMN importance real NOT NULL DEFAULT 0.5
  CHECK (importance >= 0.0 AND importance <= 1.0);

CREATE INDEX IF NOT EXISTS idx_episode_importance ON episode (importance DESC)
  WHERE superseded_by IS NULL;
```

Importance is set at episode creation time by a lightweight heuristic in `salience.ts`:

```typescript
interface SalienceSignals {
  readonly knowledgeNodesExtracted: number; // more nodes = richer conversation
  readonly coreMemoryUpdated: boolean;      // core memory changes are high-signal
  readonly contradictionDetected: boolean;  // conflict = important
  readonly userExplicitMarker: boolean;     // user said "remember this" or similar
}

function scoreEpisodeImportance(signals: SalienceSignals): number {
  let score = 0.5;
  if (signals.knowledgeNodesExtracted >= 3) score += 0.15;
  if (signals.coreMemoryUpdated) score += 0.2;
  if (signals.contradictionDetected) score += 0.1;
  if (signals.userExplicitMarker) score += 0.25;
  return Math.min(score, 1.0);
}
```

The agent can also set importance directly via a tool call (e.g., `update_episode_importance`).

**Consolidation gate:** Episodes with `importance >= 0.8` are never compressed by the automatic
consolidation job. They are preserved at full fidelity indefinitely. The consolidation query
becomes:

```sql
SELECT * FROM episode
WHERE created_at < now() - interval '7 days'
  AND superseded_by IS NULL
  AND importance < 0.8
ORDER BY session_id, created_at
```

### 2. Recency signal in RRF

RRF currently fuses three signals: vector similarity, FTS relevance, and graph proximity. None
capture temporal context. A node created 5 minutes ago and one created 5 years ago with the same
embedding receive the same rank.

Add a fourth CTE to the RRF query:

```sql
-- Recency CTE: rank by last_accessed_at (or created_at as fallback)
recency AS (
  SELECT id,
    ROW_NUMBER() OVER (
      ORDER BY COALESCE(last_accessed_at, created_at) DESC
    ) AS rank
  FROM node
  WHERE COALESCE(last_accessed_at, created_at) > now() - interval '30 days'
)
```

The recency signal has a lower default weight than vector or FTS (e.g., `recencyWeight: 0.3` vs
`vectorWeight: 1.0`) so it influences but does not dominate. The 30-day window prevents ancient
nodes from participating in recency ranking at all.

The final RRF score becomes:

```sql
COALESCE(vw / (k + v.rank), 0) +
COALESCE(fw / (k + f.rank), 0) +
COALESCE(gw / (k + g.rank), 0) +
COALESCE(rw / (k + r.rank), 0)
```

### 3. Node metadata column

Node bodies are unstructured text. A `person` node and a `fact` node have the same schema. This
prevents structured queries ("find all people at company X") and makes deduplication blunt (cosine
similarity only).

```sql
ALTER TABLE node ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}';
```

No index on metadata initially -- add GIN index when the first structured query pattern emerges.
The column is advisory: the body remains the embeddable/searchable text, metadata enables
structured attributes per kind.

Example usage by kind:

- `person`: `{ "company": "Acme", "role": "CTO", "relationship": "colleague" }`
- `event`: `{ "date": "2027-03-15", "location": "Lisbon" }`
- `preference`: `{ "domain": "scheduling", "strength": "strong" }`

### 4. Windowed self-model calibration

Lifetime-cumulative calibration becomes irrevocable after thousands of predictions. Recent
performance matters more for autonomy decisions.

```sql
ALTER TABLE self_model_domain
  ADD COLUMN recent_predictions integer NOT NULL DEFAULT 0
    CHECK (recent_predictions >= 0),
  ADD COLUMN recent_correct integer NOT NULL DEFAULT 0
    CHECK (recent_correct >= 0 AND recent_correct <= recent_predictions),
  ADD COLUMN window_reset_at timestamptz NOT NULL DEFAULT now();
```

The application code resets the window every 30 days (or every 50 predictions, whichever comes
first). The `recent_correct / recent_predictions` ratio is the primary signal for autonomy
graduation; the lifetime ratio is a secondary sanity check.

### 5. Kind-specific decay half-lives

A single 30-day half-life treats preferences and observations identically. Stable knowledge should
decay slower.

```typescript
const HALF_LIFE_DAYS: Record<NodeKind, number> = {
  preference: 120,  // stable personal preferences
  belief: 90,       // deeply held, slow to change
  goal: 60,         // goals shift but not quickly
  principle: Infinity, // exempt (already handled)
  pattern: Infinity,   // exempt (already handled)
  person: 90,       // people don't change often
  place: 90,        // locations are stable
  fact: 30,         // default, facts can become stale
  observation: 14,  // situational, decay fast
  event: 14,        // time-bound, decay fast
};
```

This is a pure application-code change in `forgetting.ts`. No schema migration needed.

### 6. Node provenance via source_event_id

Nodes currently have no pointer back to the event that created them. Adding `source_event_id`
enables full provenance tracing without joining through the event log.

```sql
ALTER TABLE node ADD COLUMN source_event_id text;

CREATE INDEX IF NOT EXISTS idx_node_source_event ON node (source_event_id)
  WHERE source_event_id IS NOT NULL;
```

The column is nullable (existing nodes won't have it) and stores the ULID of the
`memory.node.created` event. Set at creation time in `NodeRepository.create()`.

The `NodeCreatedData` type in `events/types.ts` does not change -- the event already has its own
ID. The node table simply stores a back-reference.

### 7. Topic-level consolidation

Session-level consolidation blends unrelated topics into averaged summaries. The episode_node
cross-references already provide the signal: episodes linked to the same node cluster belong to
the same topic.

The consolidation algorithm changes from `groupBySession` to `groupByTopic`:

```typescript
function groupByTopic(
  episodes: readonly Episode[],
  episodeNodes: ReadonlyMap<number, readonly number[]>,
): ReadonlyMap<string, readonly Episode[]> {
  // 1. Build a bipartite graph: episode <-> node
  // 2. Connected components = topic clusters
  // 3. Episodes in the same component are consolidated together
  // 4. Episodes with no nodes fall back to session grouping
}
```

This produces tighter summaries with better embeddings -- each summary covers one coherent topic
rather than a blend.

### 8. User model dimension grounding

Demote the Jungian dimensions (personality_type, shadow_patterns, archetypes,
individuation_markers) from foundational to experimental. They remain in the system but are
excluded from the system prompt's user model section until their evidence count exceeds a higher
threshold (50 instead of the standard thresholds).

Replace with empirically grounded dimensions:

- `openness` (Big Five) -- curiosity, creativity, preference for novelty
- `conscientiousness` (Big Five) -- organization, discipline, planning style
- `extraversion` (Big Five) -- social energy, communication volume
- `agreeableness` (Big Five) -- cooperation, conflict style
- `neuroticism` (Big Five) -- stress response, emotional volatility

This is a data change in the Phase 7.5 persona seed and the Phase 8 user model configuration. No
schema change needed -- `user_model_dimension` already stores arbitrary named dimensions.

## Definition of Done

- [ ] Migration `0004_memory_resilience.sql` applies cleanly
- [ ] `episode.importance` column exists with CHECK constraint and default 0.5
- [ ] `scoreEpisodeImportance()` computes salience from signals
- [ ] Consolidation skips episodes with `importance >= 0.8`
- [ ] RRF query includes recency CTE as fourth signal with configurable weight
- [ ] `node.metadata` JSONB column exists, defaults to `{}`
- [ ] `self_model_domain` has `recent_predictions`, `recent_correct`, `window_reset_at`
- [ ] Windowed calibration resets every 30 days or 50 predictions
- [ ] `forgetting.ts` uses kind-specific half-lives from `HALF_LIFE_DAYS` map
- [ ] `node.source_event_id` column exists, populated on node creation
- [ ] Consolidation groups by topic cluster (connected components via episode_node)
- [ ] Episodes with no node links fall back to session grouping
- [ ] Big Five dimensions added to user model seed
- [ ] Jungian dimensions require 50+ evidence signals before inclusion in prompt
- [ ] `just check` passes

## Test Cases

### `tests/memory/salience.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Default score | No signals | 0.5 |
| Rich conversation | 5 nodes extracted | 0.65 |
| Core memory update | Core memory changed | 0.7 |
| User explicit marker | User said "remember this" | 0.75 |
| Multiple signals | All signals active | 1.0 (capped) |

### `tests/memory/retrieval.test.ts` (additions)

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Recency boost | Two equally relevant nodes, one accessed today | Recent one ranks higher |
| Recency weight | recencyWeight = 0 | Recency signal ignored |
| Old nodes excluded | Node last accessed 60 days ago | Not in recency CTE |

### `tests/memory/consolidation.test.ts` (additions)

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| High-importance preserved | Episode with importance=0.9, 10 days old | Not consolidated |
| Low-importance compressed | Episode with importance=0.3, 10 days old | Consolidated |
| Topic grouping | 2 topics in 1 session | 2 separate summaries |
| No-node fallback | Episodes with no node links | Grouped by session |

### `tests/memory/forgetting.test.ts` (additions)

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Preference slow decay | preference node, 30 days | Importance barely changed |
| Observation fast decay | observation node, 14 days | Importance halved |
| Event fast decay | event node, 14 days | Importance halved |

### `tests/db/memory-schema.test.ts` (additions)

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| episode.importance default | INSERT episode | importance = 0.5 |
| episode.importance CHECK | INSERT with importance = 1.5 | CHECK violation |
| node.metadata default | INSERT node | metadata = {} |
| self_model_domain.recent_correct CHECK | recent_correct > recent_predictions | CHECK violation |

## Risks

**Low-medium risk.** Most changes are additive (new columns, new CTE, new config). The two
riskier items:

1. **Topic-level consolidation** changes the consolidation algorithm. Connected-component detection
   adds complexity. Mitigated by fallback to session grouping when episodes have no node links.

2. **Recency signal in RRF** adds a fourth dimension to the fused query. The weight must be tuned
   empirically. Mitigated by making the weight configurable and defaulting it low (0.3).

No existing behavior is removed. All changes are backward-compatible with existing data.
