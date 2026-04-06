# Phase 7: Hybrid Retrieval (RRF)

## Motivation

Finding relevant memories is harder than storing them. This is the crown jewel of Theo's memory
system — a single SQL query that fuses three search signals (vector similarity, full-text search,
graph traversal) using Reciprocal Rank Fusion. It's what makes Theo's recall qualitatively better
than naive vector search.

A user says "work" → vector search finds nodes about employment → graph traversal follows edges to
"project X" → reaches "deadline Friday." Full-text search catches exact terms that embeddings miss
(like "PostgreSQL 18"). RRF combines all three ranked lists so nodes appearing in multiple signals
score highest.

Without this, memory retrieval is single-dimensional. With it, Theo has associative, multi-signal
recall.

## Depends on

- **Phase 5** — Embeddings + nodes (vector search, graph edges)
- **Phase 6** — Episodic memory (episode search is similar)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/retrieval.ts` | `RetrievalService` — search method, result types, options |
| `tests/memory/retrieval.test.ts` | RRF scoring, individual signals, fusion, degradation |

## Design Decisions

### Search Interface

```typescript
interface SearchOptions {
  readonly limit?: number;          // default 10
  readonly k?: number;              // RRF constant, default 60
  readonly maxGraphHops?: number;   // default 2
  readonly vectorTopN?: number;     // seeds for graph traversal, default 20
  readonly graphSeedCount?: number; // top vector hits used as graph seeds, default 5
  readonly minScore?: number;       // minimum RRF score threshold
  readonly importanceWeight?: number;    // importance multiplier, default 0 (disabled)
  readonly kinds?: readonly NodeKind[];  // filter by node kind
}

interface SearchResult {
  readonly node: Node;
  readonly score: number;           // combined RRF score
  readonly vectorRank: number | null;
  readonly ftsRank: number | null;
  readonly graphRank: number | null;
}

class RetrievalService {
  constructor(
    private readonly sql: Sql,
    private readonly embeddings: EmbeddingService,
  ) {}

  async search(query: string, options?: SearchOptions): Promise<readonly SearchResult[]> {
    const embedding = await this.embeddings.embed(query);
    const opts = {
      limit: 10, k: 60, maxGraphHops: 2, vectorTopN: 20, graphSeedCount: 5,
      ...options,
    };
    return this.executeRrfQuery(embedding, query, opts);
  }
}
```

### The RRF Query — postgres.js Implementation

This is a single SQL query executed in one database round-trip. The full implementation uses
postgres.js tagged templates — every `${}` expression is automatically parameterized by the driver.
No positional `$1`/`$2` parameters, no string interpolation.

```typescript
async executeRrfQuery(
  embedding: Float32Array,
  queryText: string,
  opts: Required<SearchOptions>,
): Promise<readonly SearchResult[]> {
  // Wrap in a statement timeout to prevent runaway recursive CTEs.
  // The timeout is scoped to this transaction only (SET LOCAL).
  const rows = await this.sql.begin(async (tx) => {
    await tx`SET LOCAL statement_timeout = '5s'`;

    return tx`
      WITH
      -- CTE 1: Vector candidates (top N by cosine similarity)
      -- Uses the HNSW index via ORDER BY ... <=> ... LIMIT N pattern.
      vector_candidates AS (
        SELECT id, 1 - (embedding <=> ${embedding}) AS similarity
        FROM node
        WHERE embedding IS NOT NULL
          ${opts.kinds ? tx`AND kind = ANY(${opts.kinds})` : tx``}
        ORDER BY embedding <=> ${embedding}
        LIMIT ${opts.vectorTopN}
      ),

      -- CTE 2: Vector ranking
      vector_ranked AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY similarity DESC) AS rank
        FROM vector_candidates
      ),

      -- CTE 3: Full-text search candidates
      fts_candidates AS (
        SELECT id, ts_rank_cd(search_text, plainto_tsquery('english', ${queryText})) AS rank_score
        FROM node
        WHERE search_text @@ plainto_tsquery('english', ${queryText})
          ${opts.kinds ? tx`AND kind = ANY(${opts.kinds})` : tx``}
        ORDER BY rank_score DESC
        LIMIT ${opts.vectorTopN}
      ),

      -- CTE 4: FTS ranking
      fts_ranked AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY rank_score DESC) AS rank
        FROM fts_candidates
      ),

      -- CTE 5: Graph seeds (top vector hits become starting points)
      graph_seeds AS (
        SELECT id FROM vector_candidates
        ORDER BY similarity DESC
        LIMIT ${opts.graphSeedCount}
      ),

      -- CTE 6: Graph traversal (recursive BFS from seeds, up to M hops)
      -- CRITICAL: Uses UNION (not UNION ALL) to deduplicate rows.
      -- UNION ALL would cause infinite recursion on graph cycles (A→B→A→B→...),
      -- because visited nodes would be re-expanded indefinitely.
      -- UNION deduplicates the (id, depth, path_weight) tuples, preventing
      -- a node from being added to the working set more than once per depth level.
      -- Combined with the depth limit, this guarantees termination.
      graph_traversal AS (
        -- Base case: seeds at depth 0
        SELECT id, 0 AS depth, 1.0::real AS path_weight
        FROM graph_seeds

        UNION

        -- Recursive step: follow active edges
        SELECT
          CASE WHEN e.source_id = gt.id THEN e.target_id ELSE e.source_id END,
          gt.depth + 1,
          (gt.path_weight * e.weight * 0.5)::real  -- decay by 50% per hop
        FROM graph_traversal gt
        JOIN edge e ON (e.source_id = gt.id OR e.target_id = gt.id)
          AND e.valid_to IS NULL  -- active edges only
        WHERE gt.depth < ${opts.maxGraphHops}
      ),

      -- CTE 6b: Aggregate graph scores (best path weight per node)
      graph_ranked AS (
        SELECT id, MAX(path_weight) AS weight,
               ROW_NUMBER() OVER (ORDER BY MAX(path_weight) DESC) AS rank
        FROM graph_traversal
        WHERE id NOT IN (SELECT id FROM graph_seeds)  -- exclude seeds (already in vector)
        GROUP BY id
      ),

      -- CTE 7: RRF fusion
      fused AS (
        SELECT
          COALESCE(v.id, f.id, g.id) AS id,
          COALESCE(1.0 / (${opts.k} + v.rank), 0) +
          COALESCE(1.0 / (${opts.k} + f.rank), 0) +
          COALESCE(1.0 / (${opts.k} + g.rank), 0) AS rrf_score,
          v.rank AS vector_rank,
          f.rank AS fts_rank,
          g.rank AS graph_rank
        FROM vector_ranked v
        FULL OUTER JOIN fts_ranked f ON v.id = f.id
        FULL OUTER JOIN graph_ranked g ON COALESCE(v.id, f.id) = g.id
      )

      SELECT
        fused.id, fused.rrf_score, fused.vector_rank, fused.fts_rank, fused.graph_rank,
        n.*
      FROM fused
      JOIN node n ON n.id = fused.id
      ${opts.minScore ? tx`WHERE fused.rrf_score > ${opts.minScore}` : tx``}
      ORDER BY fused.rrf_score DESC
      LIMIT ${opts.limit}
    `;
  });

  return rows.map(rowToSearchResult);
}
```

Key points about the postgres.js integration:

- **Tagged templates are parameterized automatically.** Every `${...}` becomes a bind parameter. No
  SQL injection risk, no manual escaping.
- **Conditional fragments** use the `sql`/`tx` tagged template for empty fragments: `${opts.kinds ?
  tx\`AND kind = ANY(${opts.kinds})\` : tx\`\`}`. This is postgres.js's pattern for optional WHERE
  clauses.
- **`SET LOCAL statement_timeout`** is scoped to the transaction. The recursive CTE MUST have a
  timeout to prevent runaway queries on unexpected graph shapes.
- **`Float32Array`** for the embedding parameter — postgres.js + pgvector handle the type
  conversion.

### Importance Weighting in RRF

The `fused` CTE is extended to incorporate node importance as a post-retrieval multiplier:

```text
final_score = rrf_score * (1.0 + importanceWeight * node.importance)
```

When `importanceWeight` is 0 (default), scoring is identical to unweighted RRF — fully
backward-compatible. When enabled (e.g., 0.5), a node with importance=1.0 gets a 1.5x multiplier on
its RRF score while a node with importance=0.1 gets only 1.05x. This lets the forgetting curve
(Phase 13) and spreading activation influence retrieval ranking without changing the core RRF
algorithm.

### Access Tracking on Retrieval

Every successful search records an access event on returned nodes. This feeds the forgetting curve
(Phase 13) — accessed nodes decay slower.

```typescript
async search(query: string, options?: SearchOptions): Promise<readonly SearchResult[]> {
  const embedding = await this.embeddings.embed(query);
  const opts = {
    limit: 10, k: 60, maxGraphHops: 2,
    vectorTopN: 20, graphSeedCount: 5,
    importanceWeight: 0, ...options,
  };
  const results = await this.executeRrfQuery(embedding, query, opts);

  // Record access for forgetting curve (fire-and-forget, never blocks retrieval)
  const nodeIds = results.map((r) => r.node.id);
  void this.nodes.recordAccess(nodeIds).catch(() => {});

  return results;
}
```

The `recordAccess()` call is fire-and-forget — retrieval must never fail because access tracking
failed. The `void` + `.catch()` pattern prevents unhandled rejection warnings.

### Graceful Degradation

The `FULL OUTER JOIN` is the key. If there are no FTS matches (query has no indexed terms), the FTS
CTEs return empty — but vector + graph results still surface. If the graph is empty (no edges yet),
vector + FTS carry the score. The system always returns the best available results with whatever
signals are present.

### RRF Scoring

```text
score(node) = 1/(k + rank_vector) + 1/(k + rank_fts) + 1/(k + rank_graph)
```

- A node in all three signals: ~3 x 1/(k+1) at best = ~0.049 (k=60)
- A node in only one signal: ~1/(k+1) = ~0.016
- The ratio between "in all three" and "in one" is ~3x, which gives strong preference to
  multi-signal nodes while still surfacing single-signal ones

The `k` parameter controls how much being #1 vs #10 matters within each signal. Higher k = more
uniform weighting across ranks.

### UNION vs UNION ALL in Recursive CTEs

The graph traversal CTE uses `UNION` (not `UNION ALL`). This is a correctness fix, not an
optimization. `UNION ALL` in a recursive CTE does not deduplicate rows, so a cycle in the graph
(A→B→A) would cause infinite recursion until the depth limit is hit from every possible path —
exponential blowup. `UNION` deduplicates the working table, so each `(id, depth, path_weight)` tuple
appears at most once. Combined with the depth limit on `gt.depth`, this guarantees termination even
on fully-connected graphs.

## Definition of Done

- [ ] `RetrievalService.search("query")` returns results ranked by RRF score
- [ ] Results include individual signal ranks (vector, FTS, graph)
- [ ] Vector-only scenario works (no FTS matches, no graph edges)
- [ ] FTS-only scenario works (no embeddings match, no graph edges)
- [ ] Graph traversal follows active edges up to `maxGraphHops`
- [ ] Graph traversal decays weight by 50% per hop
- [ ] Graph traversal uses `UNION` (not `UNION ALL`) to prevent cycles
- [ ] FULL OUTER JOIN handles all NULL combinations without errors
- [ ] Results respect `limit`, `kinds` filter, and `minScore` threshold
- [ ] `graphSeedCount` is parameterized (not hardcoded)
- [ ] `statement_timeout` of 5s wraps the query
- [ ] Query uses postgres.js tagged templates throughout — no positional parameters
- [ ] `importanceWeight` option modulates RRF score by node importance (default 0 = disabled)
- [ ] Access tracking fires on every successful search (fire-and-forget)
- [ ] Importance weighting is backward-compatible (weight=0 produces original RRF scores)
- [ ] `just check` passes

## Test Cases

### `tests/memory/retrieval.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Vector only | Nodes with embeddings, no FTS terms match, no edges | Results ranked by vector similarity |
| FTS only | Query matches exact keywords, embeddings far apart | Results ranked by FTS relevance |
| Graph boost | Node A similar, Node B connected to A via edge | Both A and B appear, B gets graph rank |
| Multi-signal fusion | Node appears in vector + FTS | Higher score than single-signal nodes |
| Graceful degradation | Empty graph, FTS returns nothing | Falls back to vector-only results |
| Hop depth limit | Chain A→B→C→D, maxHops=2 | D not reached |
| Weight decay | Node 2 hops away | Lower graph weight than 1-hop node |
| Kind filter | Mix of "fact" and "preference" nodes | Only requested kinds returned |
| Limit respected | 20 matches, limit=5 | Only top 5 returned |
| Min score threshold | Low-scoring nodes | Filtered out |
| RRF arithmetic | Known ranks | Score matches formula exactly |
| Empty DB | No nodes at all | Empty results, no error |
| Graph cycle | A→B→A cycle, maxHops=3 | Terminates, no infinite loop, correct results |
| Graph seed count | graphSeedCount=3 | Only top 3 vector hits seed graph traversal |
| Statement timeout | Artificially slow query (if feasible) | Query aborted, error returned |
| Importance weight disabled | importanceWeight=0 | Scores identical to unweighted RRF |
| Importance weight enabled | importanceWeight=0.5, nodes with importance 1.0 and 0.2 | High-importance node scores higher |
| Access recorded | Search returns 3 nodes | All 3 have access_count incremented |
| Access failure non-blocking | recordAccess throws | Search still returns results |

### Integration tests (require seeded PostgreSQL)

| Test | Setup | Expected |
| ------ | ------- | ---------- |
| HNSW index used | 10 nodes with embeddings, EXPLAIN ANALYZE | Plan shows `idx_node_embedding` (HNSW scan) |
| GIN index used | Nodes with varied text, EXPLAIN ANALYZE | Plan shows `idx_node_search_text` (GIN scan) |
| Full pipeline | Mixed nodes + edges | Correct RRF ordering |

The EXPLAIN ANALYZE tests verify index usage. If the HNSW index is not used for the vector CTE, the
query falls back to a sequential scan — correct results but unacceptable performance at scale. The
`ORDER BY embedding <=> $1 LIMIT N` pattern is the canonical form that triggers HNSW usage.

## Risks

**High risk.** This is the most complex single query in the system.

1. **Recursive CTE cycle prevention:** The graph traversal uses `UNION` (not `UNION ALL`) to
   deduplicate rows in the working table, preventing infinite recursion on cycles. The `GROUP BY id`
   in `graph_ranked` takes the best path weight per node. The `statement_timeout` is the last line
   of defense against runaway execution.

2. **HNSW index usage:** pgvector's HNSW index may not be used if the query shape doesn't match the
   expected pattern. The `ORDER BY embedding <=> $1 LIMIT N` pattern is the canonical form that
   triggers index usage. The EXPLAIN ANALYZE integration test verifies this.

3. **FULL OUTER JOIN NULLs:** Three-way FULL OUTER JOIN produces complex NULL patterns. The
   `COALESCE(v.id, f.id, g.id)` must handle all 7 combinations (any subset of 3 can be NULL).

4. **Performance:** The recursive CTE is the bottleneck. With deep graphs and many edges, it can
   explode. The depth limit (`maxGraphHops`), the small seed set (`graphSeedCount`, default 5), and
   the `statement_timeout` constrain it.

5. **postgres.js conditional fragments:** The `${opts.kinds ? tx\`AND ...\` : tx\`\`}` pattern for
   optional WHERE clauses must be tested to confirm postgres.js handles nested tagged templates
   correctly in CTEs.

**Mitigations:**

- Build incrementally: vector-only first, add FTS, add graph, add fusion
- Test each CTE independently
- Use `EXPLAIN ANALYZE` in integration tests to verify index usage
- Keep `maxGraphHops` default low (2) and `graphSeedCount` at 5
- `statement_timeout` prevents any single query from running indefinitely
