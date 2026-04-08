/**
 * RetrievalService: hybrid search over Theo's knowledge graph.
 *
 * Fuses three signals in a single SQL query using Reciprocal Rank Fusion (RRF):
 * 1. Vector similarity (pgvector HNSW, cosine distance)
 * 2. Full-text search (tsvector/ts_rank_cd)
 * 3. Graph traversal (recursive BFS from vector seeds)
 *
 * The entire fusion is ONE database round-trip — a multi-CTE SQL query with
 * FULL OUTER JOIN to combine ranked lists. Nodes appearing in multiple signals
 * score highest. Missing signals produce empty CTEs, not errors.
 *
 * Access tracking fires after every successful search (fire-and-forget) to
 * feed the forgetting curve (Phase 13).
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EmbeddingService } from "./embeddings.ts";
import { toVectorLiteral } from "./embeddings.ts";
import { type NodeRepository, rowToNode } from "./graph/nodes.ts";
import type { Node, NodeKind } from "./graph/types.ts";

// ---------------------------------------------------------------------------
// Search options and result types
// ---------------------------------------------------------------------------

/** Options for hybrid retrieval search. All fields have sensible defaults. */
export interface SearchOptions {
	/** Maximum results to return. Default 10. */
	readonly limit?: number;
	/** RRF constant — higher k = more uniform weighting across ranks. Default 60. */
	readonly k?: number;
	/** Maximum hops for graph traversal BFS. Default 2. */
	readonly maxGraphHops?: number;
	/** Top N vector candidates to fetch. Default 20. */
	readonly vectorTopN?: number;
	/** Top vector hits used as graph traversal seeds. Default 5. */
	readonly graphSeedCount?: number;
	/** Minimum RRF score threshold — results below are filtered. */
	readonly minScore?: number;
	/** Importance multiplier. Default 0 (disabled). */
	readonly importanceWeight?: number;
	/** Filter by node kind(s). */
	readonly kinds?: readonly NodeKind[];
}

/** A single search result with RRF score and per-signal rank breakdown. */
export interface SearchResult {
	readonly node: Node;
	readonly score: number;
	readonly vectorRank: number | null;
	readonly ftsRank: number | null;
	readonly graphRank: number | null;
}

// ---------------------------------------------------------------------------
// Required defaults (merged with user-provided options)
// ---------------------------------------------------------------------------

interface ResolvedOptions {
	readonly limit: number;
	readonly k: number;
	readonly maxGraphHops: number;
	readonly vectorTopN: number;
	readonly graphSeedCount: number;
	readonly minScore: number | undefined;
	readonly importanceWeight: number;
	readonly kinds: readonly NodeKind[] | undefined;
}

function resolveOptions(options?: SearchOptions): ResolvedOptions {
	return {
		limit: options?.limit ?? 10,
		k: options?.k ?? 60,
		maxGraphHops: options?.maxGraphHops ?? 2,
		vectorTopN: options?.vectorTopN ?? 20,
		graphSeedCount: options?.graphSeedCount ?? 5,
		minScore: options?.minScore,
		importanceWeight: options?.importanceWeight ?? 0,
		kinds: options?.kinds,
	};
}

// ---------------------------------------------------------------------------
// Row to SearchResult mapping
// ---------------------------------------------------------------------------

/**
 * Parse a PostgreSQL bigint/numeric value to a JS number or null.
 *
 * ROW_NUMBER() returns bigint which postgres.js serializes as a string.
 * Aggregate expressions and arithmetic may return numeric/float8 as strings too.
 * This function handles string, number, and null inputs uniformly.
 */
function parseNumericColumn(value: unknown): number | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "number") return value;
	if (typeof value === "string") return Number(value);
	return null;
}

function rowToSearchResult(row: Record<string, unknown>, importanceWeight: number): SearchResult {
	const node = rowToNode(row);
	const rrfScore = parseNumericColumn(row["rrf_score"]) ?? 0;

	// final_score = rrf_score * (1 + weight * importance)
	const score = rrfScore * (1.0 + importanceWeight * node.importance);

	return {
		node,
		score,
		vectorRank: parseNumericColumn(row["vector_rank"]),
		ftsRank: parseNumericColumn(row["fts_rank"]),
		graphRank: parseNumericColumn(row["graph_rank"]),
	};
}

// ---------------------------------------------------------------------------
// RetrievalService
// ---------------------------------------------------------------------------

export class RetrievalService {
	constructor(
		private readonly sql: Sql,
		private readonly embeddings: EmbeddingService,
		private readonly nodes: NodeRepository,
	) {}

	/**
	 * Search the knowledge graph using hybrid retrieval with RRF fusion.
	 *
	 * Embeds the query text, then executes a single SQL query that fuses
	 * vector similarity, full-text search, and graph traversal into a
	 * unified ranked result set.
	 */
	async search(query: string, options?: SearchOptions): Promise<readonly SearchResult[]> {
		const embedding = await this.embeddings.embed(query);
		const opts = resolveOptions(options);
		const results = await this.executeRrfQuery(embedding, query, opts);

		// Record access for forgetting curve (fire-and-forget, never blocks retrieval)
		const nodeIds = results.map((r) => r.node.id);
		if (nodeIds.length > 0) {
			void this.nodes.recordAccess(nodeIds).catch(() => {});
		}

		return results;
	}

	/**
	 * Execute the RRF fusion query — a single SQL round-trip with CTEs for
	 * vector search, FTS, graph traversal, and rank fusion.
	 *
	 * Wrapped in a transaction with SET LOCAL statement_timeout to prevent
	 * runaway recursive CTEs from blocking indefinitely.
	 */
	private async executeRrfQuery(
		embedding: Float32Array,
		queryText: string,
		opts: ResolvedOptions,
	): Promise<readonly SearchResult[]> {
		const vectorLiteral = toVectorLiteral(embedding);

		// Wrap in a statement timeout to prevent runaway recursive CTEs.
		// The timeout is scoped to this transaction only (SET LOCAL).
		const rows = await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			await q`SET LOCAL statement_timeout = '5s'`;

			return q`
				WITH RECURSIVE
				-- CTE 1: Vector candidates (top N by cosine similarity)
				-- Uses the HNSW index via ORDER BY ... <=> ... LIMIT N pattern.
				vector_candidates AS (
					SELECT id, 1 - (embedding <=> ${vectorLiteral}::vector) AS similarity
					FROM node
					WHERE embedding IS NOT NULL
						${opts.kinds ? q`AND kind = ANY(${opts.kinds})` : q``}
					ORDER BY embedding <=> ${vectorLiteral}::vector
					LIMIT ${opts.vectorTopN}
				),

				-- CTE 2: Vector ranking
				vector_ranked AS (
					SELECT id, ROW_NUMBER() OVER (ORDER BY similarity DESC) AS rank
					FROM vector_candidates
				),

				-- CTE 3a: Pre-compute FTS query once (avoids double plainto_tsquery evaluation)
				fts_query AS (
					SELECT plainto_tsquery('english', ${queryText}) AS tsq
				),

				-- CTE 3b: Full-text search candidates
				fts_candidates AS (
					SELECT n.id, ts_rank_cd(n.search_text, fq.tsq) AS rank_score
					FROM node n, fts_query fq
					WHERE n.search_text @@ fq.tsq
						${opts.kinds ? q`AND n.kind = ANY(${opts.kinds})` : q``}
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
				-- UNION ALL would cause infinite recursion on graph cycles (A->B->A->B...),
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
						(gt.path_weight * e.weight * 0.5)::real
					FROM graph_traversal gt
					JOIN edge e ON (e.source_id = gt.id OR e.target_id = gt.id)
						AND e.valid_to IS NULL
					WHERE gt.depth < ${opts.maxGraphHops}
				),

				-- CTE 6b: Aggregate graph scores (best path weight per node)
				graph_ranked AS (
					SELECT id, MAX(path_weight) AS weight,
						ROW_NUMBER() OVER (ORDER BY MAX(path_weight) DESC) AS rank
					FROM graph_traversal
					WHERE id NOT IN (SELECT id FROM graph_seeds)
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
					fused.rrf_score, fused.vector_rank, fused.fts_rank, fused.graph_rank,
					n.id, n.kind, n.body, n.trust, n.confidence, n.importance,
					n.sensitivity, n.access_count, n.last_accessed_at,
					n.created_at, n.updated_at
				FROM fused
				JOIN node n ON n.id = fused.id
				${opts.minScore !== undefined ? q`WHERE fused.rrf_score > ${opts.minScore}` : q``}
				ORDER BY fused.rrf_score * (1.0 + ${opts.importanceWeight} * n.importance) DESC
				LIMIT ${opts.limit}
			`;
		});

		return rows.map((row) =>
			rowToSearchResult(row as Record<string, unknown>, opts.importanceWeight),
		);
	}
}
