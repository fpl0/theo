/**
 * TOCTOU binding — `approveProposal` with a mismatched expectedPayloadHash
 * refuses to move the proposal forward.
 *
 * This is an integration test: it exercises the real DB column and the
 * transactional state transition. The `payload_hash.test.ts` name avoids
 * the `.integration.test.ts` gating convention but the file lives under
 * `tests/proposals/` and still requires `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import {
	approveProposal,
	hashProposalPayload,
	requestProposal,
} from "../../src/proposals/store.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

describe("payload_hash binding on approve", () => {
	const pool = createTestPool();
	const bus = createTestBus(pool.sql);

	beforeAll(async () => {
		await bus.start();
	});

	afterAll(async () => {
		await bus.stop();
		await pool.end();
	});

	beforeEach(async () => {
		await cleanEventTables(pool.sql);
		await pool.sql`TRUNCATE proposal CASCADE`;
	});

	test("hashProposalPayload is deterministic and order-independent", async () => {
		const a = await hashProposalPayload({ a: 1, b: 2 });
		const b = await hashProposalPayload({ b: 2, a: 1 });
		expect(a).toBe(b);
	});

	test("approve with correct hash succeeds, with wrong hash is refused", async () => {
		// Seed a source event so the causation-chain trust resolution has a row.
		const seed = await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "seed", channel: "cli" },
			metadata: {},
		});

		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: seed.id,
				title: "test",
				summary: "test",
				kind: "memory_write",
				payload: { body: "whatever" },
				effectiveTrust: "owner",
				autonomyDomain: "memory_write",
				requiredLevel: 1,
			},
		);

		// Wrong hash → payload_hash_mismatch
		const wrong = await approveProposal(
			{ sql: pool.sql, bus },
			proposal.id,
			"user",
			"00".repeat(32),
		);
		expect(wrong.kind).toBe("payload_hash_mismatch");

		// Correct hash → approved
		const right = await approveProposal(
			{ sql: pool.sql, bus },
			proposal.id,
			"user",
			proposal.payloadHash,
		);
		expect(right.kind).toBe("approved");
	});

	test("approve without expectedPayloadHash still works (no binding)", async () => {
		const seed = await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "seed", channel: "cli" },
			metadata: {},
		});

		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: seed.id,
				title: "test",
				summary: "test",
				kind: "memory_write",
				payload: { body: "whatever" },
				effectiveTrust: "owner",
				autonomyDomain: "memory_write",
				requiredLevel: 1,
			},
		);
		const result = await approveProposal({ sql: pool.sql, bus }, proposal.id, "user");
		expect(result.kind).toBe("approved");
	});
});
