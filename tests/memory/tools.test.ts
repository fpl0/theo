import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { InferShape, SdkMcpToolDefinition } from "@anthropic-ai/claude-agent-sdk";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { Sql } from "postgres";
import { type ZodRawShape, z } from "zod";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { CoreMemoryRepository } from "../../src/memory/core.ts";
import { toVectorLiteral } from "../../src/memory/embeddings.ts";
import { EdgeRepository } from "../../src/memory/graph/edges.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import { RetrievalService } from "../../src/memory/retrieval.ts";
import { createSelfModelRepository } from "../../src/memory/self_model.ts";
import { createSkillRepository } from "../../src/memory/skills.ts";
import {
	createMemoryServer,
	linkMemoriesTool,
	type MemoryDependencies,
	memoryToolList,
	readCoreTool,
	searchMemoryTool,
	searchSkillsTool,
	storeMemoryTool,
	updateCoreTool,
	updateUserModelTool,
} from "../../src/memory/tools.ts";
import { createUserModelRepository } from "../../src/memory/user_model.ts";
import {
	cleanEventTables,
	createMockEmbeddings,
	createTestBus,
	createTestPool,
} from "../helpers.ts";

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let deps: MemoryDependencies;

const embeddings = createMockEmbeddings();

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;

	bus = createTestBus(sql);
	await bus.start();

	const nodes = new NodeRepository(sql, bus, embeddings);
	const edges = new EdgeRepository(sql, bus);
	const coreMemory = new CoreMemoryRepository(sql, bus);
	const retrieval = new RetrievalService(sql, embeddings, nodes);
	const userModel = createUserModelRepository(sql, bus);
	const selfModel = createSelfModelRepository(sql, bus);
	const skills = createSkillRepository(sql, embeddings, bus);

	deps = { nodes, edges, coreMemory, retrieval, userModel, selfModel, skills };
});

beforeEach(async () => {
	await sql`TRUNCATE node, edge, skill, user_model_dimension CASCADE`;
	await sql`UPDATE core_memory SET body = '{}'::jsonb`;
	await sql`DELETE FROM core_memory_changelog`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// Zod v4's $InferObjectOutput is structurally equal to the SDK's InferShape,
// but TypeScript cannot prove the mapping — hence the single bridge cast.
async function callTool<S extends ZodRawShape>(
	t: SdkMcpToolDefinition<S>,
	input: unknown,
): Promise<CallToolResult> {
	const schema = z.object(t.inputSchema);
	const parsed = schema.safeParse(input);
	if (!parsed.success) {
		return {
			content: [{ type: "text", text: `Zod error: ${parsed.error.message}` }],
			isError: true,
		};
	}
	return t.handler(parsed.data as InferShape<S>, undefined);
}

function firstText(result: CallToolResult): string {
	const block = result.content[0];
	if (!block || block.type !== "text") return "";
	return block.text;
}

describe("store_memory", () => {
	test("stores node with default inferred trust", async () => {
		const tool = storeMemoryTool(deps);
		const result = await callTool(tool, { kind: "fact", body: "user lives in Berlin" });

		expect(result.isError).toBeFalsy();
		expect(firstText(result)).toContain("Stored memory #");

		const rows = await sql<{ trust: string; kind: string; body: string }[]>`
			SELECT trust, kind, body FROM node WHERE body = 'user lives in Berlin'
		`;
		expect(rows.length).toBe(1);
		expect(rows[0]?.trust).toBe("inferred");
		expect(rows[0]?.kind).toBe("fact");
	});

	test("stores node with owner_confirmed trust when explicitly set", async () => {
		const tool = storeMemoryTool(deps);
		const result = await callTool(tool, {
			kind: "preference",
			body: "user prefers dark mode",
			trust: "owner_confirmed",
		});

		expect(result.isError).toBeFalsy();
		const rows = await sql<{ trust: string }[]>`
			SELECT trust FROM node WHERE body = 'user prefers dark mode'
		`;
		expect(rows[0]?.trust).toBe("owner_confirmed");
	});

	test("rejects invalid kind with zod error", async () => {
		const tool = storeMemoryTool(deps);
		const result = await callTool(tool, { kind: "invalid", body: "something" });
		expect(result.isError).toBe(true);
		expect(firstText(result)).toContain("Zod error");
	});

	test("rejects empty body with zod error", async () => {
		const tool = storeMemoryTool(deps);
		const result = await callTool(tool, { kind: "fact", body: "" });
		expect(result.isError).toBe(true);
		expect(firstText(result)).toContain("Zod error");
	});

	test("accepts pattern and principle kinds", async () => {
		const tool = storeMemoryTool(deps);
		for (const kind of ["pattern", "principle"] as const) {
			const result = await callTool(tool, { kind, body: `body for ${kind}` });
			expect(result.isError).toBeFalsy();
		}
		const rows = await sql<{ kind: string }[]>`
			SELECT kind FROM node WHERE kind IN ('pattern', 'principle') ORDER BY kind
		`;
		expect(rows.map((r) => r.kind)).toEqual(["pattern", "principle"]);
	});
});

describe("search_memory", () => {
	test("returns formatted results when matches exist", async () => {
		await deps.nodes.create({ kind: "fact", body: "TypeScript is strict", actor: "theo" });
		await deps.nodes.create({ kind: "fact", body: "TypeScript is fun", actor: "theo" });

		const tool = searchMemoryTool(deps);
		const result = await callTool(tool, { query: "TypeScript", limit: 5 });

		expect(result.isError).toBeFalsy();
		const text = firstText(result);
		expect(text).toContain("TypeScript");
		expect(text).toContain("score:");
	});

	test("returns 'No memories found.' for empty result", async () => {
		const tool = searchMemoryTool(deps);
		const result = await callTool(tool, { query: "nothing-matches-xyzzy" });
		expect(result.isError).toBeFalsy();
		expect(firstText(result)).toBe("No memories found.");
	});

	test("applies kind filter", async () => {
		await deps.nodes.create({ kind: "fact", body: "Berlin is a city", actor: "theo" });
		await deps.nodes.create({
			kind: "preference",
			body: "Berlin coffee is good",
			actor: "theo",
		});

		const tool = searchMemoryTool(deps);
		const result = await callTool(tool, {
			query: "Berlin",
			limit: 10,
			kinds: ["preference"],
		});

		expect(result.isError).toBeFalsy();
		const text = firstText(result);
		expect(text).toContain("preference");
		expect(text).not.toContain("fact]");
	});
});

describe("read_core", () => {
	test("returns all 4 slots as JSON", async () => {
		const tool = readCoreTool(deps);
		const result = await callTool(tool, {});

		expect(result.isError).toBeFalsy();
		const parsed = JSON.parse(firstText(result)) as Record<string, unknown>;
		expect(Object.keys(parsed).sort()).toEqual(["context", "goals", "persona", "userModel"]);
	});
});

describe("update_core", () => {
	test("updates slot and writes changelog", async () => {
		const tool = updateCoreTool(deps);
		const result = await callTool(tool, {
			slot: "persona",
			body: { name: "Theo", traits: ["curious"] },
		});

		expect(result.isError).toBeFalsy();
		expect(firstText(result)).toBe("Updated core memory: persona");

		const rows = await sql<{ body: unknown }[]>`
			SELECT body FROM core_memory WHERE slot = 'persona'
		`;
		expect(rows[0]?.body).toEqual({ name: "Theo", traits: ["curious"] });

		const changelogRows = await sql`
			SELECT slot FROM core_memory_changelog WHERE slot = 'persona'
		`;
		expect(changelogRows.length).toBe(1);
	});

	test("rejects invalid slot with zod error", async () => {
		const tool = updateCoreTool(deps);
		const result = await callTool(tool, { slot: "invalid", body: {} });
		expect(result.isError).toBe(true);
		expect(firstText(result)).toContain("Zod error");
	});
});

describe("link_memories", () => {
	test("creates edge between two existing nodes", async () => {
		const a = await deps.nodes.create({ kind: "fact", body: "node-a", actor: "theo" });
		const b = await deps.nodes.create({ kind: "fact", body: "node-b", actor: "theo" });

		const tool = linkMemoriesTool(deps);
		const result = await callTool(tool, {
			sourceId: a.id,
			targetId: b.id,
			label: "relates_to",
		});

		expect(result.isError).toBeFalsy();
		expect(firstText(result)).toBe(`Linked #${String(a.id)} -> #${String(b.id)} (relates_to)`);

		const rows = await sql`
			SELECT source_id, target_id, label
			FROM edge
			WHERE source_id = ${a.id} AND target_id = ${b.id}
		`;
		expect(rows.length).toBe(1);
	});

	test("returns error-as-value when node IDs do not exist", async () => {
		const tool = linkMemoriesTool(deps);
		const result = await callTool(tool, {
			sourceId: 999999,
			targetId: 999998,
			label: "relates_to",
		});

		expect(result.isError).toBe(true);
		expect(firstText(result)).toContain("Error:");
	});
});

describe("update_user_model", () => {
	test("upserts dimension with evidence", async () => {
		const tool = updateUserModelTool(deps);
		const result = await callTool(tool, {
			dimension: "communication_style",
			value: { style: "direct" },
			evidence: 2,
		});

		expect(result.isError).toBeFalsy();
		const text = firstText(result);
		expect(text).toContain("communication_style");
		expect(text).toContain("confidence:");

		const dim = await deps.userModel.getDimension("communication_style");
		expect(dim).not.toBeNull();
		expect(dim?.evidenceCount).toBe(2);
	});
});

describe("search_skills", () => {
	test("returns formatted skills ranked by trigger similarity", async () => {
		const embedding = await embeddings.embed("debugging a flaky test");
		const vectorLiteral = toVectorLiteral(embedding);

		await sql`
			INSERT INTO skill (
				name, trigger_context, trigger_embedding, strategy,
				success_count, attempt_count, version
			)
			VALUES (
				'flaky-test-debug',
				'debugging a flaky test',
				${vectorLiteral}::vector,
				'isolate the state, then add logging',
				8,
				10,
				1
			)
		`;

		const tool = searchSkillsTool(deps);
		const result = await callTool(tool, { query: "debugging a flaky test", limit: 3 });

		expect(result.isError).toBeFalsy();
		const text = firstText(result);
		expect(text).toContain("[skill #");
		expect(text).toContain("success: 80%");
		expect(text).toContain("v1");
		expect(text).toContain("Strategy: isolate the state");
	});

	test("returns 'No matching skills found.' when DB is empty", async () => {
		const tool = searchSkillsTool(deps);
		const result = await callTool(tool, { query: "anything" });
		expect(result.isError).toBeFalsy();
		expect(firstText(result)).toBe("No matching skills found.");
	});
});

describe("error as value", () => {
	test("store_memory returns isError when repository throws", async () => {
		const failingDeps: MemoryDependencies = {
			...deps,
			nodes: {
				...deps.nodes,
				create: async () => {
					throw new Error("simulated failure");
				},
			} as unknown as MemoryDependencies["nodes"],
		};
		const tool = storeMemoryTool(failingDeps);
		const result = await callTool(tool, { kind: "fact", body: "anything" });

		expect(result.isError).toBe(true);
		expect(firstText(result)).toBe("Error: simulated failure");
	});
});

describe("memoryToolList", () => {
	test("returns the 8 expected tool names", () => {
		const names = memoryToolList(deps)
			.map((t) => t.name)
			.sort();
		expect(names).toEqual([
			"link_memories",
			"read_core",
			"search_memory",
			"search_skills",
			"store_memory",
			"store_skill",
			"update_core",
			"update_user_model",
		]);
	});
});

describe("createMemoryServer", () => {
	test("returns SDK server config named 'memory'", () => {
		const server = createMemoryServer(deps);
		expect(server.type).toBe("sdk");
		expect(server.name).toBe("memory");
	});
});
