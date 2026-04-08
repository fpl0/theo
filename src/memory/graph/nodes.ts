/**
 * NodeRepository: CRUD operations for knowledge graph nodes.
 *
 * Every mutation emits an event through the bus. Embedding is attempted
 * on create/update but never blocks node storage — a null embedding is
 * acceptable and can be backfilled asynchronously.
 *
 * All mutations wrap the SQL write + event emit in a single transaction
 * to guarantee atomicity between projection state and the event log.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../../db/pool.ts";
import type { EventBus } from "../../events/bus.ts";
import type { Actor, NodeKind, Sensitivity } from "../../events/types.ts";
import type { EmbeddingService } from "../embeddings.ts";
import { fromVectorLiteral, toVectorLiteral } from "../embeddings.ts";
import type { CreateNodeInput, Node, TrustTier, UpdateNodeInput } from "./types.ts";
import { asNodeId, type NodeId } from "./types.ts";

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

/** Map a postgres.js row (snake_case columns) to a typed Node. */
function rowToNode(row: Record<string, unknown>): Node {
	const embeddingRaw = row["embedding"];
	return {
		id: asNodeId(row["id"] as number),
		kind: row["kind"] as NodeKind,
		body: row["body"] as string,
		embedding: typeof embeddingRaw === "string" ? fromVectorLiteral(embeddingRaw) : null,
		trust: row["trust"] as TrustTier,
		confidence: row["confidence"] as number,
		importance: row["importance"] as number,
		sensitivity: row["sensitivity"] as Sensitivity,
		accessCount: row["access_count"] as number,
		lastAccessedAt: (row["last_accessed_at"] as Date | null) ?? null,
		createdAt: row["created_at"] as Date,
		updatedAt: row["updated_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export class NodeRepository {
	constructor(
		private readonly sql: Sql,
		private readonly bus: EventBus,
		private readonly embeddings: EmbeddingService,
	) {}

	async create(data: CreateNodeInput): Promise<Node> {
		// Embed outside transaction — no DB I/O, avoids holding a tx open during inference.
		let vectorStr: string | null = null;
		try {
			const embedding = await this.embeddings.embed(data.body);
			vectorStr = toVectorLiteral(embedding);
		} catch {
			// Node will be created without embedding.
			// A bus handler on memory.node.created can retry embedding async.
		}

		// Atomic: INSERT + event emit in the same transaction.
		return this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				INSERT INTO node (kind, body, embedding, trust, confidence, importance, sensitivity)
				VALUES (
					${data.kind},
					${data.body},
					${vectorStr === null ? this.sql`NULL` : this.sql`${vectorStr}::vector`},
					${data.trust ?? "inferred"},
					${data.confidence ?? 1.0},
					${data.importance ?? 0.5},
					${data.sensitivity ?? "none"}
				)
				RETURNING *
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error("INSERT INTO node returned no rows");
			}
			const node = rowToNode(row as Record<string, unknown>);

			await this.bus.emit(
				{
					type: "memory.node.created",
					version: 1,
					actor: data.actor,
					data: {
						nodeId: node.id,
						kind: node.kind,
						body: node.body,
						sensitivity: node.sensitivity,
						hasEmbedding: vectorStr !== null,
					},
					metadata: data.metadata ?? {},
				},
				{ tx },
			);

			return node;
		});
	}

	async getById(id: NodeId): Promise<Node | null> {
		const rows = await this.sql`
			SELECT * FROM node WHERE id = ${id}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		return rowToNode(row);
	}

	async update(id: NodeId, data: UpdateNodeInput): Promise<Node> {
		// Read current state + embed outside transaction to avoid holding tx during inference.
		const current = await this.getById(id);
		if (current === null) {
			throw new Error(`Node ${String(id)} not found`);
		}

		let vectorStr: string | null = null;
		if (data.body !== undefined && data.body !== current.body) {
			try {
				const embedding = await this.embeddings.embed(data.body);
				vectorStr = toVectorLiteral(embedding);
			} catch {
				// Keep existing embedding if re-embed fails
			}
		}

		// Atomic: UPDATE + event emits in the same transaction.
		return this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				UPDATE node SET
					kind = COALESCE(${data.kind ?? null}, kind),
					body = COALESCE(${data.body ?? null}, body),
					embedding = COALESCE(
						${vectorStr !== null ? this.sql`${vectorStr}::vector` : this.sql`NULL`},
						embedding
					),
					trust = COALESCE(${data.trust ?? null}, trust),
					confidence = COALESCE(${data.confidence ?? null}, confidence),
					importance = COALESCE(${data.importance ?? null}, importance),
					sensitivity = COALESCE(${data.sensitivity ?? null}, sensitivity)
				WHERE id = ${id}
				RETURNING *
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error(`Node ${String(id)} not found after UPDATE`);
			}
			const node = rowToNode(row as Record<string, unknown>);

			// Emit typed events for each changed field
			if (data.body !== undefined && data.body !== current.body) {
				await this.bus.emit(
					{
						type: "memory.node.updated",
						version: 1,
						actor: data.actor,
						data: {
							nodeId: node.id,
							update: { field: "body", oldValue: current.body, newValue: data.body },
						},
						metadata: data.metadata ?? {},
					},
					{ tx },
				);
			}
			if (data.kind !== undefined && data.kind !== current.kind) {
				await this.bus.emit(
					{
						type: "memory.node.updated",
						version: 1,
						actor: data.actor,
						data: {
							nodeId: node.id,
							update: { field: "kind", oldValue: current.kind, newValue: data.kind },
						},
						metadata: data.metadata ?? {},
					},
					{ tx },
				);
			}
			if (data.sensitivity !== undefined && data.sensitivity !== current.sensitivity) {
				await this.bus.emit(
					{
						type: "memory.node.updated",
						version: 1,
						actor: data.actor,
						data: {
							nodeId: node.id,
							update: {
								field: "sensitivity",
								oldValue: current.sensitivity,
								newValue: data.sensitivity,
							},
						},
						metadata: data.metadata ?? {},
					},
					{ tx },
				);
			}
			if (data.confidence !== undefined && data.confidence !== current.confidence) {
				await this.bus.emit(
					{
						type: "memory.node.updated",
						version: 1,
						actor: data.actor,
						data: {
							nodeId: node.id,
							update: {
								field: "confidence",
								oldValue: current.confidence,
								newValue: data.confidence,
							},
						},
						metadata: data.metadata ?? {},
					},
					{ tx },
				);
			}

			return node;
		});
	}

	async adjustConfidence(id: NodeId, delta: number, actor: Actor): Promise<void> {
		// Atomic: UPDATE + event emit in the same transaction.
		await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				UPDATE node
				SET confidence = GREATEST(0.0, LEAST(1.0, confidence + ${delta}))
				WHERE id = ${id}
				RETURNING id, confidence
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error(`Node ${String(id)} not found`);
			}

			await this.bus.emit(
				{
					type: "memory.node.confidence_adjusted",
					version: 1,
					actor,
					data: { nodeId: id, delta, newConfidence: row["confidence"] as number },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	async findSimilar(
		embedding: Float32Array,
		threshold: number,
		limit: number,
	): Promise<(Node & { readonly similarity: number })[]> {
		const queryStr = toVectorLiteral(embedding);
		// Exclude embedding column from SELECT to avoid transferring ~3KB per row.
		// The HNSW index is triggered by ORDER BY + LIMIT; the threshold in WHERE
		// is applied as a post-filter after the approximate nearest neighbor scan.
		const rows = await this.sql`
			SELECT id, kind, body, trust, confidence, importance, sensitivity,
				access_count, last_accessed_at, created_at, updated_at,
				1 - (embedding <=> ${queryStr}::vector) AS similarity
			FROM node
			WHERE embedding IS NOT NULL
				AND 1 - (embedding <=> ${queryStr}::vector) >= ${threshold}
			ORDER BY embedding <=> ${queryStr}::vector
			LIMIT ${limit}
		`;
		return rows.map((row) => ({
			...rowToNode(row as Record<string, unknown>),
			similarity: row["similarity"] as number,
		}));
	}

	async recordAccess(nodeIds: readonly NodeId[]): Promise<void> {
		if (nodeIds.length === 0) return;
		const ids = nodeIds.map(Number);
		// Atomic: UPDATE + event emit in the same transaction.
		await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			await q`
				UPDATE node
				SET access_count = access_count + 1,
					last_accessed_at = now()
				WHERE id = ANY(${ids}::int[])
			`;
			await this.bus.emit(
				{
					type: "memory.node.accessed",
					version: 1,
					actor: "system",
					data: { nodeIds: ids },
					metadata: {},
				},
				{ tx },
			);
		});
	}
}
