/**
 * Abstraction hierarchy.
 *
 * During consolidation, find clusters of related same-kind nodes and
 * synthesize higher-level `pattern` nodes. Multiple related patterns become
 * a single `principle`.
 *
 * Clustering heuristic (phase-13 minimum viable version):
 *   - Find node triples of the same kind that are already connected by edges
 *     of weight ≥ 1.0 (strong co-occurrence).
 *   - Skip clusters that already have an `abstracted_from` edge pointing to
 *     an existing pattern (idempotency).
 *   - Ask the LLM to summarize the cluster as a single pattern statement.
 *   - Emit `memory.pattern.synthesized` with the source node IDs so the
 *     projection can rebuild the abstraction chain on replay. The effect
 *     handler's output is captured as a separate event downstream.
 *
 * The LLM call uses the same cost-controlled `query()` pattern as
 * contradiction classification: `model: "haiku"`, `tools: []`, `maxTurns: 1`,
 * JSON schema output. Returns `{ pattern: "NONE" }` when no clear pattern
 * exists, in which case the cluster is skipped.
 */

import type { Sql } from "postgres";
import { describeError } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import type { NodeKind } from "../events/types.ts";
import type { EdgeRepository } from "./graph/edges.ts";
import type { NodeRepository } from "./graph/nodes.ts";
import { asNodeId, type NodeId } from "./graph/types.ts";
import { cheapQuery } from "./llm.ts";

/** Minimum nodes per cluster. */
export const MIN_CLUSTER_SIZE = 3;

/** Minimum edge weight for two nodes to be considered "connected" for clustering. */
const MIN_EDGE_WEIGHT = 1.0;

/** Max clusters processed per consolidation run (bounds cost). */
const MAX_CLUSTERS_PER_RUN = 10;

/** Sentinel string the LLM returns when it cannot find a pattern. */
export const NO_PATTERN_SENTINEL = "NONE";

// ---------------------------------------------------------------------------
// LLM seam
// ---------------------------------------------------------------------------

/** Function that synthesizes a pattern statement from a list of node bodies. */
export type PatternSynthesizer = (bodies: readonly string[]) => Promise<string>;

/**
 * Default synthesizer using the Claude Agent SDK. Returns the pattern string
 * or `NO_PATTERN_SENTINEL` ("NONE") when no clear pattern exists.
 */
export async function defaultPatternSynthesizer(bodies: readonly string[]): Promise<string> {
	const bullets = bodies.map((b, i) => `${String(i + 1)}. ${b}`).join("\n");
	const { structured } = await cheapQuery({
		prompt:
			"Below are statements that frequently appear together. If they share a " +
			"clear higher-level pattern, state it in one sentence. If they do not, " +
			`respond with exactly "${NO_PATTERN_SENTINEL}".\n\n${bullets}`,
		schema: {
			type: "object",
			properties: { pattern: { type: "string" } },
			required: ["pattern"],
		},
	});
	if (typeof structured === "object" && structured !== null && "pattern" in structured) {
		const pattern = (structured as { pattern: unknown }).pattern;
		if (typeof pattern === "string" && pattern.trim().length > 0) {
			return pattern.trim();
		}
	}
	return NO_PATTERN_SENTINEL;
}

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface AbstractionDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly nodes: NodeRepository;
	readonly edges: EdgeRepository;
	readonly synthesizer?: PatternSynthesizer;
}

// ---------------------------------------------------------------------------
// Cluster discovery
// ---------------------------------------------------------------------------

interface ClusterCandidate {
	readonly aId: number;
	readonly bId: number;
	readonly cId: number;
	readonly kind: NodeKind;
}

/**
 * Find same-kind node triples connected through strong edges (weight >=
 * MIN_EDGE_WEIGHT). Excludes clusters where any pair already shares an
 * `abstracted_from` pattern — those are idempotently skipped.
 */
async function findClusters(sql: Sql): Promise<ClusterCandidate[]> {
	// Three-way self-join on edges produces triangles. The trust-tier filter
	// excludes pattern/principle rows from being members of a cluster
	// themselves — abstractions should be over ground-level nodes.
	const rows = await sql`
		SELECT DISTINCT
			na.id AS a_id,
			nb.id AS b_id,
			nc.id AS c_id,
			na.kind AS kind
		FROM edge e1
		JOIN edge e2 ON e2.source_id = e1.target_id AND e2.target_id <> e1.source_id
		JOIN edge e3 ON (e3.source_id = e1.source_id AND e3.target_id = e2.target_id)
			OR (e3.source_id = e2.target_id AND e3.target_id = e1.source_id)
		JOIN node na ON na.id = e1.source_id
		JOIN node nb ON nb.id = e1.target_id
		JOIN node nc ON nc.id = e2.target_id
		WHERE e1.valid_to IS NULL AND e2.valid_to IS NULL AND e3.valid_to IS NULL
		  AND e1.weight >= ${MIN_EDGE_WEIGHT}
		  AND e2.weight >= ${MIN_EDGE_WEIGHT}
		  AND e3.weight >= ${MIN_EDGE_WEIGHT}
		  AND na.kind = nb.kind AND nb.kind = nc.kind
		  AND na.kind NOT IN ('pattern', 'principle')
		  AND na.id < nb.id AND nb.id < nc.id
		  AND NOT EXISTS (
		    SELECT 1 FROM edge ex
		    WHERE ex.label = 'abstracted_from'
		      AND ex.valid_to IS NULL
		      AND ex.target_id IN (na.id, nb.id, nc.id)
		  )
		LIMIT ${MAX_CLUSTERS_PER_RUN}
	`;
	return rows.map((row) => ({
		aId: row["a_id"] as number,
		bId: row["b_id"] as number,
		cId: row["c_id"] as number,
		kind: row["kind"] as NodeKind,
	}));
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Synthesize pattern nodes for eligible clusters. Returns the number of
 * pattern nodes created.
 *
 * This is called from `consolidate()`. It is not a bus handler — the
 * consolidation job's orchestration is the scheduling authority.
 */
export async function synthesizeAbstractions(deps: AbstractionDeps): Promise<number> {
	const synthesizer = deps.synthesizer ?? defaultPatternSynthesizer;
	const clusters = await findClusters(deps.sql);

	let created = 0;
	for (const cluster of clusters) {
		const sourceIds: readonly NodeId[] = [
			asNodeId(cluster.aId),
			asNodeId(cluster.bId),
			asNodeId(cluster.cId),
		];
		const nodes = await Promise.all(sourceIds.map((id) => deps.nodes.getById(id)));
		if (nodes.some((n) => n === null)) continue;
		const bodies = (nodes as { body: string }[]).map((n) => n.body);

		let patternText: string;
		try {
			patternText = await synthesizer(bodies);
		} catch (error: unknown) {
			console.warn(`Pattern synthesizer failed: ${describeError(error)}`);
			continue;
		}
		if (patternText === NO_PATTERN_SENTINEL || patternText.length === 0) continue;

		const patternNode = await deps.nodes.create({
			kind: "pattern",
			body: patternText,
			importance: 0.7,
			actor: "system",
		});

		// Link each source node back to the pattern so retrieval can traverse
		// the abstraction. Uses EdgeRepository for atomic INSERT + event emit.
		await Promise.all(
			sourceIds.map((sourceId) =>
				deps.edges.create({
					sourceId: patternNode.id,
					targetId: sourceId,
					label: "abstracted_from",
					weight: 1.0,
					actor: "system",
				}),
			),
		);

		await deps.bus.emit({
			type: "memory.pattern.synthesized",
			version: 1,
			actor: "system",
			data: {
				patternNodeId: patternNode.id,
				sourceNodeIds: sourceIds,
				kind: "pattern",
			},
			metadata: {},
		});
		created++;
	}

	// Principle extraction: if multiple patterns themselves share strong edges,
	// recurse one level. Bounded to one extra pattern per run.
	created += await synthesizePrinciplesFrom(deps, synthesizer);

	return created;
}

/**
 * Look for existing pattern nodes that are themselves edge-connected and
 * distill them into a single `principle`. Runs at most once per
 * consolidation.
 */
async function synthesizePrinciplesFrom(
	deps: AbstractionDeps,
	synthesizer: PatternSynthesizer,
): Promise<number> {
	const rows = await deps.sql`
		SELECT na.id AS a_id, nb.id AS b_id
		FROM edge e
		JOIN node na ON na.id = e.source_id AND na.kind = 'pattern'
		JOIN node nb ON nb.id = e.target_id AND nb.kind = 'pattern'
		WHERE e.valid_to IS NULL AND e.weight >= ${MIN_EDGE_WEIGHT}
		  AND NOT EXISTS (
		    SELECT 1 FROM edge ex
		    WHERE ex.label = 'abstracted_from'
		      AND ex.valid_to IS NULL
		      AND ex.target_id IN (na.id, nb.id)
		      AND EXISTS (
		        SELECT 1 FROM node np WHERE np.id = ex.source_id AND np.kind = 'principle'
		      )
		  )
		LIMIT 1
	`;
	const pair = rows[0];
	if (pair === undefined) return 0;

	const a = await deps.nodes.getById(asNodeId(pair["a_id"] as number));
	const b = await deps.nodes.getById(asNodeId(pair["b_id"] as number));
	if (a === null || b === null) return 0;

	let principle: string;
	try {
		principle = await synthesizer([a.body, b.body]);
	} catch (error: unknown) {
		console.warn(`Principle synthesizer failed: ${describeError(error)}`);
		return 0;
	}
	if (principle === NO_PATTERN_SENTINEL || principle.length === 0) return 0;

	const principleNode = await deps.nodes.create({
		kind: "principle",
		body: principle,
		importance: 0.8,
		actor: "system",
	});
	await Promise.all(
		[a.id, b.id].map((targetId) =>
			deps.edges.create({
				sourceId: principleNode.id,
				targetId,
				label: "abstracted_from",
				weight: 1.0,
				actor: "system",
			}),
		),
	);
	await deps.bus.emit({
		type: "memory.pattern.synthesized",
		version: 1,
		actor: "system",
		data: {
			patternNodeId: principleNode.id,
			sourceNodeIds: [a.id, b.id],
			kind: "principle",
		},
		metadata: {},
	});
	return 1;
}
