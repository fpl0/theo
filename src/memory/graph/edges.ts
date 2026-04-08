/**
 * EdgeRepository: CRUD operations for knowledge graph edges.
 *
 * Edges are temporally versioned. Updating an edge = expire the old one
 * (set valid_to = now()) + create a new one. Full history is preserved.
 * Active edges have valid_to IS NULL.
 *
 * All mutations wrap the SQL write + event emit in a single transaction
 * to guarantee atomicity between projection state and the event log.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../../db/pool.ts";
import type { EventBus } from "../../events/bus.ts";
import type { Actor } from "../../events/types.ts";
import type { CreateEdgeInput, Edge, UpdateEdgeInput } from "./types.ts";
import { asEdgeId, asNodeId, type EdgeId, type NodeId } from "./types.ts";

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

/** Map a postgres.js row (snake_case columns) to a typed Edge. */
function rowToEdge(row: Record<string, unknown>): Edge {
	return {
		id: asEdgeId(row["id"] as number),
		sourceId: asNodeId(row["source_id"] as number),
		targetId: asNodeId(row["target_id"] as number),
		label: row["label"] as string,
		weight: row["weight"] as number,
		validFrom: row["valid_from"] as Date,
		validTo: (row["valid_to"] as Date | null) ?? null,
		createdAt: row["created_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export class EdgeRepository {
	constructor(
		private readonly sql: Sql,
		private readonly bus: EventBus,
	) {}

	async create(data: CreateEdgeInput): Promise<Edge> {
		// Atomic: INSERT + event emit in the same transaction.
		return this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				INSERT INTO edge (source_id, target_id, label, weight)
				VALUES (${data.sourceId}, ${data.targetId}, ${data.label}, ${data.weight ?? 1.0})
				RETURNING *
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error("INSERT INTO edge returned no rows");
			}
			const edge = rowToEdge(row as Record<string, unknown>);

			await this.bus.emit(
				{
					type: "memory.edge.created",
					version: 1,
					actor: data.actor,
					data: {
						edgeId: edge.id,
						sourceId: edge.sourceId,
						targetId: edge.targetId,
						label: edge.label,
						weight: edge.weight,
					},
					metadata: data.metadata ?? {},
				},
				{ tx },
			);

			return edge;
		});
	}

	async expire(id: EdgeId, actor: Actor): Promise<void> {
		// Atomic: UPDATE + event emit in the same transaction.
		await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				UPDATE edge SET valid_to = now()
				WHERE id = ${id} AND valid_to IS NULL
				RETURNING id
			`;
			if (rows.length === 0) {
				throw new Error(`Active edge ${String(id)} not found`);
			}

			await this.bus.emit(
				{
					type: "memory.edge.expired",
					version: 1,
					actor,
					data: { edgeId: id },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	async update(id: EdgeId, data: UpdateEdgeInput): Promise<Edge> {
		return this.sql.begin(async (tx) => {
			const q = asQueryable(tx);

			// Get the current edge to copy unchanged fields
			const currentRows = await q`
				SELECT * FROM edge WHERE id = ${id} AND valid_to IS NULL FOR UPDATE
			`;
			const currentRow = currentRows[0];
			if (currentRow === undefined) {
				throw new Error(`Active edge ${String(id)} not found`);
			}

			// Expire old edge (guard ensures idempotency under concurrent access)
			await q`UPDATE edge SET valid_to = now() WHERE id = ${id} AND valid_to IS NULL`;
			await this.bus.emit(
				{
					type: "memory.edge.expired",
					version: 1,
					actor: data.actor,
					data: { edgeId: id },
					metadata: data.metadata ?? {},
				},
				{ tx },
			);

			// Create new edge with updated fields
			const rows = await q`
				INSERT INTO edge (source_id, target_id, label, weight)
				VALUES (
					${currentRow["source_id"]},
					${currentRow["target_id"]},
					${data.label ?? currentRow["label"]},
					${data.weight ?? currentRow["weight"]}
				)
				RETURNING *
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error("INSERT INTO edge returned no rows during update");
			}
			const edge = rowToEdge(row as Record<string, unknown>);

			await this.bus.emit(
				{
					type: "memory.edge.created",
					version: 1,
					actor: data.actor,
					data: {
						edgeId: edge.id,
						sourceId: edge.sourceId,
						targetId: edge.targetId,
						label: edge.label,
						weight: edge.weight,
					},
					metadata: data.metadata ?? {},
				},
				{ tx },
			);

			return edge;
		});
	}

	async getActiveForNode(nodeId: NodeId): Promise<Edge[]> {
		const rows = await this.sql`
			SELECT * FROM edge
			WHERE (source_id = ${nodeId} OR target_id = ${nodeId})
				AND valid_to IS NULL
			ORDER BY created_at DESC
		`;
		return rows.map((row) => rowToEdge(row as Record<string, unknown>));
	}

	async getById(id: EdgeId): Promise<Edge | null> {
		const rows = await this.sql`
			SELECT * FROM edge WHERE id = ${id}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		return rowToEdge(row as Record<string, unknown>);
	}
}
