# Hybrid Retrieval with RRF Fusion (FPL-24)

## Context

M1 retrieval was pure vector similarity via `search_nodes()` in `nodes.py` — a single `ORDER BY embedding <=> $1` query. The `node` table already has a GIN-indexed `tsv` tsvector column for full-text search, and the `edge` table (FPL-20) supports graph traversal. M2 fuses all three signals into a single ranked list.

## Decision: Reciprocal Rank Fusion in a single SQL query

RRF scores each node as `SUM(1/(k + rank_i))` across signals where it appears. This was chosen over learned-weight fusion because it requires no training data, handles missing signals gracefully (a node absent from a signal simply gets zero contribution), and is well-established in information retrieval literature. The constant `k=60` is configurable via `Settings.retrieval_rrf_k`.

The entire fusion runs as a single SQL query with CTEs: `vector_ranked`, `fts_ranked`, `graph_seeds`, `graph_traversal`, `graph_deduped`, `graph_ranked`, and `rrf_fused`. This avoids multiple round-trips and lets PostgreSQL's optimizer see the full plan.

## Decision: Graph traversal seeded from top vector hits

The graph CTE seeds from the top-N vector results (configurable via `retrieval_graph_seed_count`, default 5) rather than from FTS results. Vector search is the strongest signal for semantic relevance, so seeding from it produces the most useful graph neighbors. The traversal depth is bounded by `retrieval_graph_max_depth` (default 2) with cycle prevention via path tracking.

## Decision: `DISTINCT ON` + `ROW_NUMBER` for graph deduplication

Graph traversal can reach the same node via multiple paths with different cumulative weights. A `DISTINCT ON (node_id) ORDER BY cumulative_weight DESC` step picks the best path per node before `ROW_NUMBER` assigns a rank. This two-step approach (dedup then rank) ensures stable ranking.

## Decision: Per-signal boolean flags instead of counting queries

Rather than running separate counting queries for observability, the RRF query returns `in_vector`, `in_fts`, and `in_graph` boolean columns alongside each result. The caller counts them in Python for OTEL span attributes. This trades slightly wider result rows for eliminating 3-5 extra database round-trips.

## Decision: Two static SQL variants for kind filtering

Like `nodes.py`, there are two SQL constants: `_HYBRID_SEARCH` (no kind filter) and `_HYBRID_SEARCH_BY_KIND` (with `$8` kind filter applied in both `vector_ranked` and `fts_ranked` CTEs). Static SQL is preferred over dynamic construction for plan caching and auditability. The kind filter is not applied to the graph CTE because graph neighbors may be of different kinds and still be relevant.

## Decision: Config in Settings, not function parameters

RRF tuning parameters (`retrieval_rrf_k`, `retrieval_candidate_limit`, `retrieval_graph_seed_count`, `retrieval_graph_max_depth`) live in `Settings` rather than as function arguments. These are deployment-level tuning knobs, not per-call choices. The public API stays clean: `hybrid_search(query, *, limit, kind)`.

## Files changed

- `src/theo/config.py` — added retrieval settings (`retrieval_rrf_k`, `retrieval_candidate_limit`, `retrieval_graph_seed_count`, `retrieval_graph_max_depth`)
- `src/theo/memory/retrieval.py` — new module with `hybrid_search` function
- `tests/test_retrieval.py` — unit tests for fusion, degradation, kind filter, config, ordering
- `docs/decisions/hybrid-retrieval.md` — this file
