/**
 * EpisodicRepository: conversation message storage with embeddings.
 *
 * Episodes are append-only. Every append computes an embedding (outside
 * the transaction — CPU-bound and idempotent), then atomically INSERTs
 * the episode and emits a `memory.episode.created` event in a single
 * database transaction.
 *
 * Consolidation (Phase 13) will create summary episodes and mark originals
 * via superseded_by. The getBySession query already filters these out.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { EmbeddingService } from "./embeddings.ts";
import { toVectorLiteral } from "./embeddings.ts";
import type { CreateEpisodeInput, Episode, EpisodeId } from "./types.ts";
import { asEpisodeId } from "./types.ts";

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

/** Map a postgres.js row (snake_case columns) to a typed Episode. */
function rowToEpisode(row: Record<string, unknown>): Episode {
	const supersededBy = row["superseded_by"] as number | null;
	return {
		id: asEpisodeId(row["id"] as number),
		sessionId: row["session_id"] as string,
		role: row["role"] as Episode["role"],
		body: row["body"] as string,
		embedding: null, // Embedding excluded from queries to avoid transferring ~3KB per row
		supersededBy: supersededBy !== null ? asEpisodeId(supersededBy) : null,
		createdAt: row["created_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export class EpisodicRepository {
	constructor(
		private readonly sql: Sql,
		private readonly bus: EventBus,
		private readonly embeddings: EmbeddingService,
	) {}

	/**
	 * Append a new episode with embedding. Embedding is computed outside the
	 * transaction (CPU-bound, idempotent). INSERT + event emission are atomic.
	 */
	async append(input: CreateEpisodeInput): Promise<Episode> {
		const embedding = await this.embeddings.embed(input.body);
		const vectorStr = toVectorLiteral(embedding);

		const episode = await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q`
				INSERT INTO episode (session_id, role, body, embedding)
				VALUES (${input.sessionId}, ${input.role}, ${input.body}, ${vectorStr}::vector)
				RETURNING id, session_id, role, body, superseded_by, created_at
			`;
			const row = rows[0];
			if (row === undefined) {
				throw new Error("INSERT INTO episode returned no rows");
			}

			await this.bus.emit(
				{
					type: "memory.episode.created",
					version: 1,
					actor: input.actor,
					data: {
						episodeId: (row as Record<string, unknown>)["id"] as number,
						sessionId: input.sessionId,
						role: input.role,
					},
					metadata: { sessionId: input.sessionId },
				},
				{ tx },
			);

			return rowToEpisode(row as Record<string, unknown>);
		});

		return episode;
	}

	/**
	 * Get all non-superseded episodes for a session in chronological order.
	 * Superseded episodes (consolidated in Phase 13) are excluded.
	 */
	async getBySession(sessionId: string): Promise<readonly Episode[]> {
		const rows = await this.sql`
			SELECT id, session_id, role, body, superseded_by, created_at
			FROM episode
			WHERE session_id = ${sessionId} AND superseded_by IS NULL
			ORDER BY created_at ASC
		`;
		return rows.map((row) => rowToEpisode(row as Record<string, unknown>));
	}

	/**
	 * Link an episode to a knowledge graph node. Idempotent — duplicate
	 * links are silently ignored via ON CONFLICT DO NOTHING.
	 */
	async linkToNode(episodeId: EpisodeId, nodeId: number): Promise<void> {
		await this.sql`
			INSERT INTO episode_node (episode_id, node_id)
			VALUES (${episodeId}, ${nodeId})
			ON CONFLICT DO NOTHING
		`;
	}
}
