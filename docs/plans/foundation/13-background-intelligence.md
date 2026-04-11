# Phase 13: Background Intelligence

## Cross-cutting dependencies

This phase owns two cross-cutting amendments that 12a and 13b depend on:

1. **Decision / effect handler mode** (`foundation.md §7.4`). The bus from Phase 3 gains
   a `HandlerMode = "decision" | "effect"` flag on handler registration. Decision
   handlers run on both live dispatch and replay; effect handlers run only in live mode.
   This phase implements the bus amendment **as a precondition to its own LLM handlers**
   (contradiction detection, episode summarization).
2. **LLM-driven handlers split into `*_requested` + `*_classified`** event pairs:
   - `contradiction.requested` (decision, stores the request) +
     `contradiction.classified` (effect, stores the LLM output). Downstream decision
     handlers read the classified event to create the `contradicts` edge or adjust
     confidence.
   - `episode.summarize_requested` (decision, enumerates the episodes) +
     `episode.summarized` (effect, stores the full summary text). Downstream decision
     handlers update `episode.superseded_by` from the event data.

   This guarantees replay determinism: the outside world's answer is an event, not a
   call. See `foundation.md §7.4` for the rationale.

Phases 12a and 13b consume both amendments. 12a's executive loop is registered as an
effect handler; its projection handlers are decision handlers. 13b's reflex and ideation
flows follow the same pattern.

## Motivation

Background intelligence is what makes Theo's memory system alive rather than a static store. Three
mechanisms run autonomously:

1. **Contradiction detection** -- When a new fact is stored, Theo checks if it contradicts existing
   knowledge. Conflicts are surfaced, not silently overwritten. This prevents the knowledge graph
   from accumulating inconsistencies over years.

2. **Auto-edges** -- When concepts co-occur in the same conversation turn, they get linked in the
   graph. Over time, the knowledge graph self-organizes -- frequently discussed topics become
   strongly connected, improving future retrieval without explicit linking.

3. **Consolidation** -- Old episodes are compressed into summaries. Duplicate nodes are merged.
   Projection snapshots are captured. This prevents unbounded growth while preserving the important
   information.

Without these, the memory system degrades over time -- contradictions pile up, related concepts stay
disconnected, and storage grows without bound.

## Depends on

- **Phase 5** -- Knowledge graph (nodes, similarity search, `NodeRepository.adjustConfidence()`)
- **Phase 6** -- Episodic memory (episodes, episode_node links)
- **Phase 7** -- Retrieval (similar node search)
- **Phase 12** -- Scheduler (consolidation runs as a built-in job)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/contradiction.ts` | Contradiction detection handler |
| `src/memory/auto_edges.ts` | Auto-edge discovery from co-occurrence |
| `src/memory/consolidation.ts` | Episode compression, node deduplication, snapshots |
| `tests/memory/contradiction.test.ts` | Detection, confidence adjustment, edge creation |
| `tests/memory/auto_edges.test.ts` | Co-occurrence detection, weight saturation |
| `tests/memory/consolidation.test.ts` | Episode compression, deduplication |
| `src/memory/forgetting.ts` | Forgetting curve decay logic |
| `src/memory/propagation.ts` | Importance propagation on retrieval neighbors |
| `src/memory/abstraction.ts` | Pattern and principle synthesis from node clusters |
| `tests/memory/forgetting.test.ts` | Decay computation, access frequency modification |
| `tests/memory/propagation.test.ts` | Hop traversal, importance boost, normalization |
| `tests/memory/abstraction.test.ts` | Cluster detection, pattern/principle creation |

### Events added

This phase adds two new event types to the Phase 2 union:

- `memory.node.merged` -- Emitted when two near-duplicate nodes are merged during consolidation.
- `memory.node.importance.propagated` -- Importance boosted on graph neighbors after retrieval.

## Design Decisions

### Contradiction Detection

A bus handler on `memory.node.created`:

```typescript
bus.on("memory.node.created", async (event) => {
  await detectContradictions(event.data.nodeId, deps);
}, { id: "contradiction-detector" });
```

#### Cost Controls

Contradiction detection uses LLM calls. Explicit rate limiting prevents runaway costs:

```typescript
class ContradictionDetector {
  private callsThisMinute = 0;
  private readonly maxCallsPerMinute = 10;

  async detect(nodeId: NodeId): Promise<void> {
    if (this.callsThisMinute >= this.maxCallsPerMinute) return; // skip, will retry next time
    this.callsThisMinute++;
    // ... detection logic
  }
}
```

All classification calls use `model: "haiku"` (cheapest available). Each call is bounded by the
structured output schema -- responses are small.

#### Detection Flow

```typescript
async function detectContradictions(nodeId: NodeId, deps: ContradictionDeps): Promise<void> {
  const node = await deps.nodes.getById(nodeId);
  if (!node || !node.embedding) return;

  // Find semantically similar nodes of the same kind
  const similar = await deps.nodes.findSimilar(node.embedding, 0.8, 5);
  const candidates = similar.filter((n) => n.id !== nodeId && n.kind === node.kind);

  if (candidates.length === 0) return;

  // Ask Claude for contradiction classification (fire-and-forget, never blocks)
  for (const candidate of candidates) {
    const classification = await classifyContradiction(node, candidate);

    if (classification.contradicts) {
      // Reduce confidence on both nodes
      // Uses NodeRepository.adjustConfidence() defined in Phase 5
      await deps.nodes.adjustConfidence(nodeId, -0.2);
      await deps.nodes.adjustConfidence(candidate.id, -0.2);

      // Create contradiction edge with explanation
      await deps.edges.create({
        sourceId: nodeId,
        targetId: candidate.id,
        label: "contradicts",
        weight: 1.0,
        actor: "system",
      });

      await deps.bus.emit({
        type: "memory.contradiction.detected",
        version: 1,
        actor: "system",
        data: {
          nodeId,
          conflictId: candidate.id,
          explanation: classification.explanation,
        },
        metadata: {},
      });
    }
  }
}
```

#### Contradiction Classification via SDK `query()`

Uses the verified `query()` API with structured output. The async generator must be fully consumed:

```typescript
async function classifyContradiction(
  a: Node,
  b: Node,
): Promise<{ contradicts: boolean; explanation: string }> {
  const classificationQuery = query({
    prompt: `Do these two statements contradict each other?\n\nA: "${a.body}"\nB: "${b.body}"`,
    options: {
      model: "haiku",
      tools: [],
      persistSession: false,
      maxTurns: 1,
      settingSources: [],
      permissionMode: "bypassPermissions",
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
      },
    },
  });

  for await (const message of classificationQuery) {
    if (message.type === "result" && message.subtype === "success") {
      return message.structured_output as { contradicts: boolean; explanation: string };
    }
  }

  // If we get here, the query produced no successful result
  return { contradicts: false, explanation: "classification failed" };
}
```

### Auto-Edge Discovery

A bus handler on `turn.completed`:

```typescript
bus.on("turn.completed", async (event) => {
  await discoverAutoEdges(event.metadata.sessionId, deps);
}, { id: "auto-edge-discovery" });
```

```typescript
async function discoverAutoEdges(sessionId: string | undefined, deps: AutoEdgeDeps): Promise<void> {
  if (!sessionId) return;

  // Find all node pairs that co-occurred in this session's episodes
  const pairs = await deps.sql`
    SELECT a.node_id AS source_id, b.node_id AS target_id, COUNT(*) AS co_count
    FROM episode_node a
    JOIN episode_node b ON a.episode_id = b.episode_id AND a.node_id < b.node_id
    JOIN episode e ON e.id = a.episode_id
    WHERE e.session_id = ${sessionId}
    GROUP BY a.node_id, b.node_id
  `;

  for (const pair of pairs) {
    // Find existing co_occurs edge
    const [existing] = await deps.sql`
      SELECT id, weight FROM edge
      WHERE source_id = ${pair.source_id} AND target_id = ${pair.target_id}
        AND label = 'co_occurs' AND valid_to IS NULL
    `;

    if (existing) {
      // Strengthen existing edge (saturate at weight 5.0)
      const newWeight = Math.min(5.0, existing.weight + pair.co_count * 0.5);
      if (newWeight > existing.weight) {
        await deps.edges.update(existing.id, { weight: newWeight });
      }
    } else {
      // Create new co_occurs edge
      await deps.edges.create({
        sourceId: pair.source_id,
        targetId: pair.target_id,
        label: "co_occurs",
        weight: Math.min(5.0, pair.co_count * 0.5),
        actor: "system",
      });
    }
  }
}
```

Weight saturation: co-occurrence edges cap at weight 5.0, reached after ~10 co-occurrences (0.5 per
co-occurrence). This prevents runaway weights from distorting RRF graph scores.

### Forgetting Curves

Exponential decay on node importance, modified by access frequency. Runs as part of the
consolidation job (every 6 hours).

Key design decisions:

- **Half-life**: 30 days base. Access frequency extends it: `effective_half_life = base * (1 +
  access_count * 0.1)`. A node accessed 10 times has 2x the half-life.
- **Floor at 0.05**: Nodes never fully disappear. Even forgotten nodes can resurface if directly
  searched.
- **Pattern/principle exempt**: Abstract nodes synthesized by the abstraction hierarchy are immune
  to decay — they represent distilled knowledge.
- **Consolidation integration**: The decay pass runs after episode compression and deduplication,
  before abstraction synthesis.

### Importance Propagation

When nodes are retrieved by RRF search, their graph neighbors get a small importance boost. This
simulates spreading activation from cognitive science.

A bus handler on `turn.completed` finds recently accessed nodes and boosts their 1-2 hop neighbors:

- 1-hop neighbors: importance += 0.02 * edge_weight
- 2-hop neighbors: importance += 0.01 * edge_weight

The delta is small enough that propagation alone cannot push a node to high importance — it needs
repeated activation from retrieval.

**Normalization**: The consolidation job normalizes importance periodically — if mean importance
drifts above 0.6, all importances are scaled back to a 0.5 mean. This prevents runaway inflation
while preserving relative ordering.

### Abstraction Hierarchy

During consolidation, the system identifies clusters of related nodes and synthesizes higher-level
`pattern` and `principle` nodes.

1. **Pattern detection**: Find 3+ same-kind nodes with strong edges or high co-occurrence.
   Synthesize a `pattern` node using LLM (haiku model, same pattern as contradiction
   classification). Link to source nodes with `abstracted_from` edges.

2. **Principle extraction**: Query existing pattern nodes. If multiple patterns point in the same
   direction, synthesize a `principle` node. Principles are rare — maybe dozens after a year.

3. **Idempotency**: Before synthesizing, check if source nodes already have an `abstracted_from`
   edge from an existing pattern. Skip if already abstracted.

4. **LLM call**: Uses `query()` with `model: "haiku"`, `tools: []`, `maxTurns: 1` — same
   cost-controlled pattern as contradiction detection. Returns "NONE" if no clear pattern exists.

### Consolidation

Runs as a scheduled job (Phase 12). The consolidation logic lives in this phase -- the consolidator
subagent (Phase 14) is the *scheduler job* that invokes this logic, but the logic itself is
standalone and uses direct `query()` calls for summarization.

```typescript
async function consolidate(deps: ConsolidationDeps): Promise<ConsolidationResult> {
  const result = { episodesCompressed: 0, nodesMerged: 0, snapshotsCaptured: 0 };

  // 1. Compress old episodes (older than 7 days)
  const oldEpisodes = await deps.sql`
    SELECT * FROM episode
    WHERE created_at < now() - interval '7 days'
      AND superseded_by IS NULL
    ORDER BY session_id, created_at
  `;

  // Group by session, summarize each session's episodes
  const sessions = groupBySession(oldEpisodes);
  for (const [sessionId, episodes] of sessions) {
    const summary = await summarizeEpisodes(episodes);
    const consolidated = await deps.episodic.append({
      sessionId,
      role: "assistant",
      body: summary,
      actor: "system",
    });
    // Mark originals as superseded
    for (const ep of episodes) {
      await deps.sql`UPDATE episode SET superseded_by = ${consolidated.id} WHERE id = ${ep.id}`;
    }
    result.episodesCompressed += episodes.length;
  }

  // 2. Deduplicate similar nodes (cosine similarity > 0.95)
  const duplicates = await deps.sql`
    SELECT a.id AS id_a, b.id AS id_b, 1 - (a.embedding <=> b.embedding) AS similarity
    FROM node a
    JOIN node b ON a.id < b.id AND a.kind = b.kind
    WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
      AND 1 - (a.embedding <=> b.embedding) > 0.95
    ORDER BY similarity DESC
    LIMIT 50
  `;
  // Merge: keep higher-confidence node, redirect edges, supersede the other
  for (const dup of duplicates) {
    await mergeNodes(dup.id_a, dup.id_b, deps);
    result.nodesMerged++;
  }

  // 3. Capture projection snapshots
  // ... (future: serialize key projection state + last event cursor)
  result.snapshotsCaptured = 1;

  // 3. Apply forgetting curves
  result.nodesDecayed = await applyForgettingCurves(deps);

  // 4. Normalize importance (prevent unbounded propagation drift)
  // Scale all importances proportionally if mean > 0.6

  // 5. Synthesize abstractions (patterns from clusters, principles from patterns)
  result.abstractionsSynthesized = await synthesizeAbstractions(deps);

  return result;
}
```

#### Episode Summarization

`summarizeEpisodes()` uses a direct `query()` call -- no subagent needed. This is a lightweight
classification task:

```typescript
async function summarizeEpisodes(episodes: readonly Episode[]): Promise<string> {
  const transcript = episodes.map((e) => `[${e.role}]: ${e.body}`).join("\n");

  const summarizeQuery = query({
    prompt: "Summarize this conversation into a concise " +
      "paragraph preserving key facts, decisions, " +
      `and action items:\n\n${transcript}`,
    options: {
      model: "haiku",
      tools: [],
      persistSession: false,
      maxTurns: 1,
      settingSources: [],
      permissionMode: "bypassPermissions",
    },
  });

  for await (const message of summarizeQuery) {
    if (message.type === "result" && message.subtype === "success") {
      return extractTextFromResult(message);
    }
  }
  return "Summary generation failed.";
}
```

#### Node Merging

Full SQL transaction for merging near-duplicate nodes:

```typescript
async function mergeNodes(keepId: NodeId, mergeId: NodeId, deps: MergeDeps): Promise<void> {
  await deps.sql.begin(async (tx) => {
    // 1. Redirect edges from mergeId to keepId
    await tx`UPDATE edge SET source_id = ${keepId} WHERE source_id = ${mergeId} AND valid_to IS NULL`;
    await tx`UPDATE edge SET target_id = ${keepId} WHERE target_id = ${mergeId} AND valid_to IS NULL`;

    // 2. Redirect episode_node references
    await tx`UPDATE episode_node SET node_id = ${keepId} WHERE node_id = ${mergeId}
             ON CONFLICT (episode_id, node_id) DO NOTHING`;

    // 3. Take the higher confidence/importance
    await tx`UPDATE node SET
      confidence = GREATEST(n.confidence, m.confidence),
      importance = GREATEST(n.importance, m.importance)
      FROM node n, node m
      WHERE n.id = ${keepId} AND m.id = ${mergeId} AND node.id = ${keepId}`;

    // 4. Soft-delete the merged node (set confidence to 0, add merged_into edge)
    await tx`UPDATE node SET confidence = 0 WHERE id = ${mergeId}`;

    // 5. Emit event
    await deps.bus.emit({
      type: "memory.node.merged", version: 1, actor: "system",
      data: { keptId: keepId, mergedId: mergeId },
      metadata: {},
    }, { tx });
  });
}
```

This requires adding `memory.node.merged` to the event union (Phase 2).

### Cascade Analysis

Background handlers react to events, which could trigger further events. Analysis of the cascade
depth:

- `memory.node.created` --> contradiction detector --> may emit `memory.edge.created` +
  `memory.contradiction.detected`
- `memory.edge.created` --> no handlers registered for this event (safe)
- `memory.contradiction.detected` --> no handlers registered (safe)

- `turn.completed` --> importance propagation --> emits `memory.node.importance.propagated` (safe,
  no handlers)
- `memory.node.decayed` --> no handlers (safe)
- `memory.pattern.synthesized` --> no handlers (safe)

**Conclusion:** Maximum cascade depth is 2. No infinite loops are possible. The contradiction
detector only listens on `memory.node.created`, and the events it emits (`memory.edge.created`,
`memory.contradiction.detected`) have no registered handlers that create further nodes. The new
forgetting, propagation, and abstraction events are terminal — no handlers are registered for them.

## Definition of Done

- [ ] Contradiction detection fires on `memory.node.created`
- [ ] Similar nodes of the same kind are checked for contradiction
- [ ] Contradicting nodes get reduced confidence (via `NodeRepository.adjustConfidence()` from Phase
  5) and a `contradicts` edge
- [ ] `memory.contradiction.detected` event emitted with explanation
- [ ] Contradiction detection never blocks the node creation path
- [ ] Contradiction detection rate-limited to 10 calls/minute with `model: "haiku"`
- [ ] Auto-edges fire on `turn.completed`
- [ ] Co-occurring nodes in the same session get `co_occurs` edges
- [ ] Existing co_occurs edges are strengthened (not duplicated)
- [ ] Weight saturates at 5.0
- [ ] Consolidation compresses episodes older than 7 days
- [ ] Consolidated episodes link to originals via `superseded_by`
- [ ] `summarizeEpisodes()` uses direct `query()` with `model: "haiku"`, no subagent
- [ ] Near-duplicate nodes (>0.95 similarity) are merged via full SQL transaction
- [ ] `memory.node.merged` event emitted within the merge transaction
- [ ] Cascade depth verified: max 2, no infinite loops
- [ ] `applyForgettingCurves()` decays importance with exponential curve modified by access_count
- [ ] Decay floor at 0.05 — nodes never fully disappear
- [ ] Pattern and principle nodes exempt from decay
- [ ] Importance propagation boosts 1-2 hop neighbors by delta=0.02/hops
- [ ] Consolidation normalizes importance to prevent unbounded drift
- [ ] `synthesizeAbstractions()` finds clusters of 3+ related nodes, creates pattern nodes
- [ ] Pattern nodes linked to source nodes via `abstracted_from` edges
- [ ] All three new event types emitted correctly
- [ ] `just check` passes

## Test Cases

### `tests/memory/contradiction.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| No similar nodes | New node, no matches | No contradiction check |
| Non-contradicting | Similar but consistent nodes | No edges, no confidence change |
| Contradicting | "User likes cats" vs "User dislikes cats" | Confidence reduced, contradicts edge, event |
| Different kinds ignored | Similar text but different kinds | No comparison |
| Multiple candidates | 3 similar nodes | Each checked independently |
| Rate limit | 11 nodes in one minute | Only first 10 checked |

### `tests/memory/auto_edges.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| New co-occurrence | Two nodes in same episode | `co_occurs` edge created |
| Strengthen existing | Same pair co-occurs again | Weight increased |
| Weight saturation | 20 co-occurrences | Weight = 5.0, not higher |
| No self-edges | Node with itself | No edge (a.node_id < b.node_id) |
| Cross-session | Nodes in different sessions | No edge for this tick |

### `tests/memory/consolidation.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Old episodes compressed | Episodes 10 days old | Summary created, originals superseded |
| Recent episodes kept | Episodes 2 days old | Not touched |
| Near-duplicates merged | Two nodes, 0.96 similarity | One merged into the other |
| Edges redirected | Merged node had edges | Edges point to surviving node |
| Merge event emitted | Two nodes merged | `memory.node.merged` in event log |
| Idempotent | Run twice | Second run finds nothing to do |

### `tests/memory/forgetting.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Basic decay | Node with importance=0.5, no accesses | Importance reduced |
| Access resistance | Node with access_count=10 vs 0 | High-access decays slower |
| Floor respected | Node with importance=0.06 | Decays to 0.05, not lower |
| Pattern exempt | Pattern node | Not decayed |
| Principle exempt | Principle node | Not decayed |

### `tests/memory/propagation.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| 1-hop boost | Node A retrieved, B connected | B importance increases by ~0.02 |
| 2-hop boost | A→B→C chain | C importance increases by ~0.01 |
| No self-boost | Node A retrieved | A not changed by propagation |
| Importance capped | Node near 1.0 | Capped at 1.0 |

### `tests/memory/abstraction.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Pattern synthesized | 3 related fact nodes | Pattern node created with abstracted_from edges |
| No pattern | 3 unrelated nodes | No pattern created |
| Already abstracted | Cluster has existing pattern | Skipped |
| LLM returns NONE | No clear pattern | No node created |

## Risks

**Medium risk.**

1. **Contradiction detection uses LLM calls** -- cost, latency, non-determinism. Rate-limited to 10
   calls/minute. Uses `model: "haiku"` (cheapest). Each call is fire-and-forget with error catching
   -- failures are logged, not retried.

2. **Auto-edge query performance** -- The self-join on `episode_node` for co-occurrence can be
   expensive with many episodes. The `session_id` filter constrains it to one session at a time,
   which should be manageable.

3. **Consolidation is destructive** -- Once episodes are superseded, the summaries replace them in
   queries. The originals are preserved (not deleted) but hidden. If summarization loses important
   nuance, it's recoverable but requires manual intervention.

**Mitigations:**

- Contradiction detection is fire-and-forget with error catching
- Auto-edge handler has its own checkpoint -- failures don't block other handlers
- Consolidation uses `superseded_by` (not DELETE) -- originals are always recoverable
