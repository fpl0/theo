import type { Sql } from "postgres";
import type { EmbeddingService } from "./embeddings.ts";
import { toVectorLiteral } from "./embeddings.ts";

export interface Skill {
	readonly id: number;
	readonly name: string;
	readonly trigger: string;
	readonly strategy: string;
	readonly successRate: number;
	readonly version: number;
}

// Every selected column is NOT NULL per schema; success_rate is a generated real.
function rowToSkill(row: Record<string, unknown>): Skill {
	return {
		id: row["id"] as number,
		name: row["name"] as string,
		trigger: row["trigger_context"] as string,
		strategy: row["strategy"] as string,
		successRate: Number(row["success_rate"] ?? 0),
		version: row["version"] as number,
	};
}

export interface SkillRepository {
	findByTrigger(query: string, limit: number): Promise<readonly Skill[]>;
}

export function createSkillRepository(sql: Sql, embeddings: EmbeddingService): SkillRepository {
	async function findByTrigger(query: string, limit: number): Promise<readonly Skill[]> {
		const embedding = await embeddings.embed(query);
		const vectorLiteral = toVectorLiteral(embedding);

		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, trigger_context, strategy, success_rate, version
			FROM skill
			WHERE trigger_embedding IS NOT NULL
				AND promoted_at IS NULL
			ORDER BY trigger_embedding <=> ${vectorLiteral}::vector, success_rate DESC
			LIMIT ${limit}
		`;

		return rows.map(rowToSkill);
	}

	return { findByTrigger };
}
