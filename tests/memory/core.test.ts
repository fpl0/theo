/**
 * Integration tests for CoreMemoryRepository.
 *
 * Tests read, readSlot, update with changelog, hash computation,
 * and concurrent update behavior.
 *
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { CoreMemoryRepository } from "../../src/memory/core.ts";
import { SlotNotFoundError } from "../../src/memory/types.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: CoreMemoryRepository;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;

	bus = createTestBus(sql);
	await bus.start();

	repo = new CoreMemoryRepository(sql, bus);
});

beforeEach(async () => {
	// Reset core_memory slots to empty objects (the seeded default)
	await sql`UPDATE core_memory SET body = '{}'::jsonb`;
	await sql`TRUNCATE core_memory_changelog CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("CoreMemoryRepository.read", () => {
	test("returns all 4 slots with seeded empty objects", async () => {
		const core = await repo.read();

		expect(core.persona).toEqual({});
		expect(core.goals).toEqual({});
		expect(core.userModel).toEqual({});
		expect(core.context).toEqual({});
	});

	test("returns updated values after modification", async () => {
		await repo.update("persona", { name: "Theo", role: "assistant" }, "system");
		await repo.update("goals", ["help user", "learn preferences"], "system");

		const core = await repo.read();
		expect(core.persona).toEqual({ name: "Theo", role: "assistant" });
		expect(core.goals).toEqual(["help user", "learn preferences"]);
	});
});

describe("CoreMemoryRepository.readSlot", () => {
	test("returns ok result for existing slot", async () => {
		const result = await repo.readSlot("persona");

		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.value).toEqual({});
		}
	});

	test("returns updated value after modification", async () => {
		await repo.update("goals", { primary: "assist user" }, "system");

		const result = await repo.readSlot("goals");
		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.value).toEqual({ primary: "assist user" });
		}
	});

	test("returns error for deleted slot", async () => {
		// Temporarily remove a slot to simulate corruption
		await sql`DELETE FROM core_memory WHERE slot = 'context'`;

		const result = await repo.readSlot("context");
		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.error).toBeInstanceOf(SlotNotFoundError);
			expect(result.error.slot).toBe("context");
		}

		// Re-insert to not break other tests
		await sql`INSERT INTO core_memory (slot, body) VALUES ('context', '{}')`;
	});
});

describe("CoreMemoryRepository.update", () => {
	test("updates slot body and emits event atomically", async () => {
		await repo.update("persona", { name: "Theo" }, "system");

		// Slot updated
		const result = await repo.readSlot("persona");
		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.value).toEqual({ name: "Theo" });
		}

		// Event emitted
		const events = await sql`
			SELECT * FROM events WHERE type = 'memory.core.updated'
			ORDER BY id DESC LIMIT 1
		`;
		expect(events.length).toBe(1);
		const data = (events[0] as Record<string, unknown>)["data"] as Record<string, unknown>;
		expect(data["slot"]).toBe("persona");
		expect(data["changedBy"]).toBe("system");
	});

	test("records changelog with before and after values", async () => {
		await repo.update("goals", { version: 1 }, "user");
		await repo.update("goals", { version: 2 }, "theo");

		const logs = await sql`
			SELECT * FROM core_memory_changelog
			WHERE slot = 'goals'
			ORDER BY created_at ASC
		`;
		expect(logs.length).toBe(2);

		// First changelog: {} -> { version: 1 }
		const log1 = logs[0] as Record<string, unknown>;
		expect(log1["body_before"]).toEqual({});
		expect(log1["body_after"]).toEqual({ version: 1 });
		expect(log1["changed_by"]).toBe("user");

		// Second changelog: { version: 1 } -> { version: 2 }
		const log2 = logs[1] as Record<string, unknown>;
		expect(log2["body_before"]).toEqual({ version: 1 });
		expect(log2["body_after"]).toEqual({ version: 2 });
		expect(log2["changed_by"]).toBe("theo");
	});

	test("throws SlotNotFoundError for deleted slot", async () => {
		await sql`DELETE FROM core_memory WHERE slot = 'context'`;

		await expect(repo.update("context", { data: "test" }, "system")).rejects.toThrow(
			SlotNotFoundError,
		);

		// Re-insert
		await sql`INSERT INTO core_memory (slot, body) VALUES ('context', '{}')`;
	});
});

describe("CoreMemoryRepository.hash", () => {
	test("returns a string hash", async () => {
		const hash = await repo.hash();
		expect(typeof hash).toBe("string");
		expect(hash.length).toBeGreaterThan(0);
	});

	test("hash is stable when nothing changes", async () => {
		const hash1 = await repo.hash();
		const hash2 = await repo.hash();
		expect(hash1).toBe(hash2);
	});

	test("hash changes when a slot is updated", async () => {
		const before = await repo.hash();
		await repo.update("persona", { changed: true }, "system");
		const after = await repo.hash();
		expect(before).not.toBe(after);
	});

	test("hash changes when any slot is updated", async () => {
		const h1 = await repo.hash();
		await repo.update("context", { session: "new" }, "system");
		const h2 = await repo.hash();
		await repo.update("goals", ["goal1"], "system");
		const h3 = await repo.hash();

		expect(h1).not.toBe(h2);
		expect(h2).not.toBe(h3);
	});
});

describe("concurrent updates", () => {
	test("sequential updates to same slot preserve changelog ordering", async () => {
		await repo.update("persona", { step: 1 }, "user");
		await repo.update("persona", { step: 2 }, "theo");
		await repo.update("persona", { step: 3 }, "system");

		const logs = await sql`
			SELECT * FROM core_memory_changelog
			WHERE slot = 'persona'
			ORDER BY created_at ASC
		`;
		expect(logs.length).toBe(3);

		// Each changelog's body_after should match the next one's body_before
		const log0 = logs[0] as Record<string, unknown>;
		const log1 = logs[1] as Record<string, unknown>;
		const log2 = logs[2] as Record<string, unknown>;

		expect(log0["body_after"]).toEqual({ step: 1 });
		expect(log1["body_before"]).toEqual({ step: 1 });
		expect(log1["body_after"]).toEqual({ step: 2 });
		expect(log2["body_before"]).toEqual({ step: 2 });
		expect(log2["body_after"]).toEqual({ step: 3 });
	});
});
