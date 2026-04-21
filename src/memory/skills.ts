/**
 * Procedural Memory — Skill Repository.
 *
 * Skills are learned strategies with trigger embeddings, a strategy body,
 * and a running success rate. They are retrieved by trigger similarity
 * (not RRF — see .claude/rules/memory.md). The reflector subagent creates
 * and refines them; the consolidation job promotes proven ones.
 *
 * Lifecycle:
 *   created  → reflector stores a strategy with a trigger phrase
 *   refined  → new row with parent_id pointing to the predecessor
 *   promoted → `promoted_at` set; excluded from active retrieval and
 *              folded into the persona by the consolidator
 *
 * Every mutation emits an event through the bus — `memory.skill.created`
 * on create/refine, `memory.skill.promoted` on promotion. `recordOutcome`
 * does not emit events (it would swamp the log with hot-path noise).
 */

import type { Sql } from "postgres";
import type { EventBus } from "../events/bus.ts";
import { type EmbeddingService, toVectorLiteral } from "./embeddings.ts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Skill {
	readonly id: number;
	readonly name: string;
	readonly trigger: string;
	readonly strategy: string;
	readonly successRate: number;
	readonly successCount: number;
	readonly attemptCount: number;
	readonly version: number;
	readonly parentId: number | null;
	readonly promotedAt: Date | null;
}

/** Input for creating (or refining) a skill. */
export interface CreateSkillInput {
	readonly name: string;
	readonly trigger: string;
	readonly strategy: string;
	/**
	 * When refining: id of the predecessor skill. The new row inherits
	 * `version = parent.version + 1` automatically; the caller only
	 * supplies the parent.
	 */
	readonly parentId?: number;
}

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

// Every selected column is NOT NULL per the schema except `parent_id` and
// `promoted_at`. The Number() coercion on success_rate guards against the
// `real` column coming back as a string from postgres.js in some configs.
function rowToSkill(row: Record<string, unknown>): Skill {
	return {
		id: row["id"] as number,
		name: row["name"] as string,
		trigger: row["trigger_context"] as string,
		strategy: row["strategy"] as string,
		successRate: Number(row["success_rate"] ?? 0),
		successCount: row["success_count"] as number,
		attemptCount: row["attempt_count"] as number,
		version: row["version"] as number,
		parentId: (row["parent_id"] as number | null) ?? null,
		promotedAt: (row["promoted_at"] as Date | null) ?? null,
	};
}

const SKILL_COLUMNS =
	"id, name, trigger_context, strategy, success_rate, success_count, attempt_count, version, parent_id, promoted_at";

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export interface SkillRepository {
	/** Create a new skill or a refined version of an existing one. */
	create(input: CreateSkillInput): Promise<Skill>;
	/**
	 * Find active (non-promoted) skills whose trigger embedding is closest
	 * to the query. Ordered by cosine distance, then success rate.
	 */
	findByTrigger(query: string, limit: number): Promise<readonly Skill[]>;
	/** Increment attempt count; increment success count when success is true. */
	recordOutcome(id: number, success: boolean): Promise<Skill>;
	/** Mark a skill as promoted to the persona. Returns the updated row. */
	promote(id: number): Promise<Skill>;
	/** Return a single skill by id, or null if none exists. */
	getById(id: number): Promise<Skill | null>;
}

export function createSkillRepository(
	sql: Sql,
	embeddings: EmbeddingService,
	bus: EventBus,
): SkillRepository {
	async function create(input: CreateSkillInput): Promise<Skill> {
		const embedding = await embeddings.embed(input.trigger);
		const vectorLiteral = toVectorLiteral(embedding);

		// Version lineage: refined skills inherit version = parent + 1. The
		// SELECT is executed by postgres.js inside the same connection, which
		// means it sees a consistent view for the duration of the call.
		let version = 1;
		if (input.parentId !== undefined) {
			const parentRows = await sql<{ version: number }[]>`
				SELECT version FROM skill WHERE id = ${input.parentId}
			`;
			const parent = parentRows[0];
			if (parent === undefined) {
				throw new Error(`parent skill #${String(input.parentId)} not found`);
			}
			version = parent.version + 1;
		}

		const rows = await sql<Record<string, unknown>[]>`
			INSERT INTO skill (name, trigger_context, trigger_embedding, strategy, version, parent_id)
			VALUES (
				${input.name},
				${input.trigger},
				${vectorLiteral}::vector,
				${input.strategy},
				${version},
				${input.parentId ?? null}
			)
			RETURNING ${sql.unsafe(SKILL_COLUMNS)}
		`;
		const row = rows[0];
		if (row === undefined) {
			throw new Error("skill insert returned no row");
		}
		const skill = rowToSkill(row);

		await bus.emit({
			type: "memory.skill.created",
			version: 1,
			actor: "theo",
			data: { skillId: skill.id, name: skill.name, trigger: skill.trigger },
			metadata: {},
		});

		return skill;
	}

	async function findByTrigger(query: string, limit: number): Promise<readonly Skill[]> {
		const embedding = await embeddings.embed(query);
		const vectorLiteral = toVectorLiteral(embedding);

		// Promoted skills are excluded from active retrieval — their strategy
		// has been folded into the persona. Active skills carry the current
		// learning surface.
		const rows = await sql<Record<string, unknown>[]>`
			SELECT ${sql.unsafe(SKILL_COLUMNS)}
			FROM skill
			WHERE trigger_embedding IS NOT NULL
				AND promoted_at IS NULL
			ORDER BY trigger_embedding <=> ${vectorLiteral}::vector, success_rate DESC
			LIMIT ${limit}
		`;

		return rows.map(rowToSkill);
	}

	async function recordOutcome(id: number, success: boolean): Promise<Skill> {
		// Atomic counter bump in SQL so concurrent outcome recordings never
		// lose updates. No event — outcomes are hot-path noise.
		const rows = await sql<Record<string, unknown>[]>`
			UPDATE skill
			SET
				attempt_count = attempt_count + 1,
				success_count = success_count + ${success ? 1 : 0}
			WHERE id = ${id}
			RETURNING ${sql.unsafe(SKILL_COLUMNS)}
		`;
		const row = rows[0];
		if (row === undefined) {
			throw new Error(`skill #${String(id)} not found`);
		}
		return rowToSkill(row);
	}

	async function promote(id: number): Promise<Skill> {
		// Idempotent — promoting an already-promoted skill is a no-op; the
		// partial update preserves the original promoted_at.
		const rows = await sql<Record<string, unknown>[]>`
			UPDATE skill
			SET promoted_at = COALESCE(promoted_at, now())
			WHERE id = ${id}
			RETURNING ${sql.unsafe(SKILL_COLUMNS)}
		`;
		const row = rows[0];
		if (row === undefined) {
			throw new Error(`skill #${String(id)} not found`);
		}
		const skill = rowToSkill(row);

		await bus.emit({
			type: "memory.skill.promoted",
			version: 1,
			actor: "system",
			data: { skillId: skill.id, promotedTo: "persona" },
			metadata: {},
		});

		return skill;
	}

	async function getById(id: number): Promise<Skill | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT ${sql.unsafe(SKILL_COLUMNS)}
			FROM skill
			WHERE id = ${id}
		`;
		const row = rows[0];
		return row === undefined ? null : rowToSkill(row);
	}

	return { create, findByTrigger, recordOutcome, promote, getById };
}
