/**
 * Ideation integration tests — full run against the real DB + event log.
 *
 * Verifies:
 *   - Without consent, runIdeationJob silently skips (no events).
 *   - With consent, a full run emits scheduled + proposed + proposal.requested.
 *   - Duplicate hashes emit ideation.duplicate_suppressed instead of a new
 *     proposal.
 *   - Budget exceedance emits ideation.budget_exceeded.
 *   - Provenance filter excludes nodes at external trust tier.
 */

import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import {
	type Candidate,
	DEFAULT_BUDGET,
	type IdeationRunner,
	runIdeationJob,
	sampleCandidates,
} from "../../src/ideation/run.ts";
import { grantAutonomousCloudEgress } from "../../src/memory/egress.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

const pool = createTestPool();

async function seedNode(
	body: string,
	trust: "owner" | "owner_confirmed" | "external" = "owner",
	importance = 0.5,
): Promise<number> {
	const embed = `[${new Array(768).fill(0).join(",")}]`;
	const rows = await pool.sql<{ id: number }[]>`
		INSERT INTO node (kind, body, embedding, importance, access_count,
		                  last_accessed_at, trust, confidence, sensitivity)
		VALUES ('fact', ${body}, ${embed}::vector(768), ${importance}, 5,
		        now() - interval '30 days', ${trust}, 0.7, 'none')
		RETURNING id
	`;
	const id = rows[0]?.id;
	if (id === undefined) throw new Error("seed node failed");
	return id;
}

function fakeRunner(proposalText: string): IdeationRunner {
	return {
		async run(input): Promise<{
			proposalText: string;
			referencedNodeIds: readonly number[];
			confidence: number;
			inputTokens: number;
			outputTokens: number;
			costUsd: number;
			iterations: readonly {
				kind: "executor" | "advisor_message";
				model: string;
				inputTokens: number;
				outputTokens: number;
				costUsd: number;
			}[];
		}> {
			return {
				proposalText,
				referencedNodeIds: input.candidates.map((c: Candidate) => c.nodeId),
				confidence: 0.7,
				inputTokens: 1000,
				outputTokens: 200,
				costUsd: 0.01,
				iterations: [
					{
						kind: "executor",
						model: input.model,
						inputTokens: 1000,
						outputTokens: 200,
						costUsd: 0.01,
					},
				],
			};
		},
	};
}

beforeEach(async () => {
	await pool.sql`DELETE FROM proposal`;
	await pool.sql`DELETE FROM ideation_run`;
	await pool.sql`DELETE FROM consent_ledger`;
	await pool.sql`DELETE FROM edge`;
	await pool.sql`DELETE FROM node`;
	await cleanEventTables(pool.sql);
});

afterAll(async () => {
	await pool.end();
});

describe("sampleCandidates (provenance filter)", () => {
	test("excludes nodes at external trust tier", async () => {
		await seedNode("owner fact", "owner");
		await seedNode("external webhook content", "external");
		const candidates = await sampleCandidates(pool.sql, 10);
		const bodies = candidates.map((c) => c.body);
		expect(bodies).toContain("owner fact");
		expect(bodies).not.toContain("external webhook content");
	});

	test("excludes goal kind nodes (anti-recursion)", async () => {
		const embed = `[${new Array(768).fill(0).join(",")}]`;
		await pool.sql`
			INSERT INTO node (kind, body, embedding, importance, access_count,
			                  last_accessed_at, trust, confidence, sensitivity)
			VALUES ('goal', 'existing goal', ${embed}::vector(768), 0.9, 5,
			        now(), 'owner', 0.9, 'none')
		`;
		await seedNode("fact not goal", "owner");
		const candidates = await sampleCandidates(pool.sql, 10);
		expect(candidates.map((c) => c.body)).not.toContain("existing goal");
		expect(candidates.map((c) => c.body)).toContain("fact not goal");
	});

	test("excludes ideation-origin nodes (metadata.origin = 'ideation')", async () => {
		const embed = `[${new Array(768).fill(0).join(",")}]`;
		await pool.sql`
			INSERT INTO node (kind, body, embedding, importance, access_count,
			                  last_accessed_at, trust, confidence, sensitivity, metadata)
			VALUES ('fact', 'own dream', ${embed}::vector(768), 0.9, 5,
			        now(), 'owner', 0.9, 'none',
			        ${pool.sql.json({ origin: "ideation" } as never)})
		`;
		await seedNode("non-ideation origin", "owner");
		const candidates = await sampleCandidates(pool.sql, 10);
		expect(candidates.map((c) => c.body)).not.toContain("own dream");
		expect(candidates.map((c) => c.body)).toContain("non-ideation origin");
	});
});

describe("runIdeationJob", () => {
	test("without consent: silently skips (no events emitted)", async () => {
		const bus = createTestBus(pool.sql);
		await bus.start();
		try {
			await seedNode("seed 1", "owner");
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("some idea"),
			});
			const rows = await pool.sql<{ count: string }[]>`
				SELECT count(*)::text AS count FROM events
				WHERE type LIKE 'ideation.%' OR type = 'proposal.requested'
			`;
			expect(Number(rows[0]?.count ?? 0)).toBe(0);
		} finally {
			await bus.stop();
		}
	});

	test("with consent: emits scheduled + proposed + proposal.requested", async () => {
		const bus = createTestBus(pool.sql);
		await bus.start();
		try {
			await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
			await seedNode("useful fact", "owner");
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("build the telescope"),
			});
			await bus.flush();

			const types = await pool.sql<{ type: string }[]>`
				SELECT type FROM events
				WHERE type IN ('ideation.scheduled','ideation.proposed','proposal.requested')
				ORDER BY id ASC
			`;
			const typeList = types.map((t) => t.type);
			expect(typeList).toContain("ideation.scheduled");
			expect(typeList).toContain("ideation.proposed");
			expect(typeList).toContain("proposal.requested");
		} finally {
			await bus.stop();
		}
	});

	test("duplicate hash: emits ideation.duplicate_suppressed", async () => {
		const bus = createTestBus(pool.sql);
		await bus.start();
		try {
			await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
			await seedNode("s", "owner");
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("same idea"),
			});
			// Reduce the per-week cap so a second run doesn't hit the budget gate.
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("same idea"),
				budget: { ...DEFAULT_BUDGET, maxRunsPerWeek: 10 },
			});

			const rows = await pool.sql<{ type: string }[]>`
				SELECT type FROM events WHERE type = 'ideation.duplicate_suppressed'
			`;
			expect(rows.length).toBeGreaterThanOrEqual(1);
		} finally {
			await bus.stop();
		}
	});

	test("with consent: emits cloud_egress.turn audit record after the run", async () => {
		const bus = createTestBus(pool.sql);
		await bus.start();
		try {
			await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
			await seedNode("another fact", "owner");
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("think about the telescope"),
			});
			await bus.flush();
			const rows = await pool.sql<{ data: Record<string, unknown> }[]>`
				SELECT data FROM events WHERE type = 'cloud_egress.turn'
			`;
			expect(rows.length).toBe(1);
			const data = rows[0]?.data ?? {};
			expect(data["turnClass"]).toBe("ideation");
			expect(data["subagent"]).toBe("ideation");
			expect(Number(data["costUsd"] ?? 0)).toBeGreaterThan(0);
		} finally {
			await bus.stop();
		}
	});

	test("budget exceedance: emits ideation.budget_exceeded and skips", async () => {
		const bus = createTestBus(pool.sql);
		await bus.start();
		try {
			await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
			// Seed a prior run count above the cap.
			for (let i = 0; i < 5; i++) {
				await pool.sql`
					INSERT INTO ideation_run (run_id, started_at, cost_usd, status)
					VALUES (${`seed-${String(i)}`}, now(), 0, 'completed')
				`;
			}
			await runIdeationJob({
				sql: pool.sql,
				bus,
				runner: fakeRunner("ignored"),
			});
			const rows = await pool.sql<{ type: string }[]>`
				SELECT type FROM events WHERE type = 'ideation.budget_exceeded'
			`;
			expect(rows.length).toBe(1);
		} finally {
			await bus.stop();
		}
	});
});
