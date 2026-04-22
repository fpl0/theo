/**
 * Phase 13b integration tests — proposals, consent ledger, degradation.
 *
 * Exercises the real SQL projections: proposal state machine, sweepExpired,
 * ideation-origin hard cap, consent grant/revoke, degradation transitions.
 * Runs against the test database (requires `just up` + `just test-db`).
 */

import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { readDegradation, setDegradation } from "../../src/degradation/state.ts";
import {
	AUTONOMOUS_CLOUD_EGRESS,
	grantAutonomousCloudEgress,
	readConsent,
	revokeAutonomousCloudEgress,
} from "../../src/memory/egress.ts";
import {
	approveProposal,
	getProposal,
	IDEATION_MAX_LEVEL,
	listPending,
	rejectProposal,
	requestProposal,
	sweepExpired,
} from "../../src/proposals/store.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

const pool = createTestPool();
const bus = createTestBus(pool.sql);

beforeEach(async () => {
	// Make each test a clean slate: clear proposals, consent, degradation
	// and event log. The degradation table has a singleton row we restore.
	await pool.sql`DELETE FROM proposal`;
	await pool.sql`DELETE FROM consent_ledger`;
	await cleanEventTables(pool.sql);
	await pool.sql`
		INSERT INTO degradation_state (id, level, reason)
		VALUES ('singleton', 0, 'initial')
		ON CONFLICT (id) DO UPDATE SET level = 0, reason = 'initial'
	`;
});

afterAll(async () => {
	await pool.end();
});

describe("proposal lifecycle", () => {
	test("request → approve emits correct events", async () => {
		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: "cause-1",
				title: "do the thing",
				summary: "the thing gets done",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "owner",
				autonomyDomain: "test.domain",
				requiredLevel: 1,
			},
		);
		expect(proposal.status).toBe("pending");

		await approveProposal({ sql: pool.sql, bus }, proposal.id, "user");
		const reloaded = await getProposal(pool.sql, proposal.id);
		expect(reloaded?.status).toBe("approved");
		expect(reloaded?.decidedBy).toBe("user");
	});

	test("request → reject emits correct events", async () => {
		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "ideation",
				sourceCauseId: "cause-2",
				title: "autonomous idea",
				summary: "Theo thinks X",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "inferred",
				autonomyDomain: "test.domain",
				requiredLevel: 2,
			},
		);
		await rejectProposal({ sql: pool.sql, bus }, proposal.id, "user", "nope");
		const reloaded = await getProposal(pool.sql, proposal.id);
		expect(reloaded?.status).toBe("rejected");
	});

	test("ideation origin hard-capped at level 2 (§11)", async () => {
		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "ideation",
				sourceCauseId: "cause-3",
				title: "tries to escalate",
				summary: "ideation asks for level 4",
				kind: "code_change",
				payload: {},
				effectiveTrust: "inferred",
				autonomyDomain: "code.review",
				requiredLevel: 4, // requested but capped
			},
		);
		expect(proposal.requiredLevel).toBe(IDEATION_MAX_LEVEL);
		expect(proposal.requiredLevel).toBeLessThanOrEqual(2);
	});

	test("non-ideation origins respect requested level", async () => {
		const proposal = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: "cause-4",
				title: "owner request",
				summary: "",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "owner",
				autonomyDomain: "code.review",
				requiredLevel: 4,
			},
		);
		expect(proposal.requiredLevel).toBe(4);
	});

	test("sweepExpired transitions pending rows past expiry to expired", async () => {
		const pastExpiry = new Date(Date.now() - 60_000);
		await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "reflex",
				sourceCauseId: "cause-5",
				title: "stale",
				summary: "",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "external",
				autonomyDomain: "test.domain",
				requiredLevel: 1,
				expiresAt: pastExpiry,
			},
		);
		const expiredIds = await sweepExpired({ sql: pool.sql, bus }, new Date());
		expect(expiredIds.length).toBe(1);
	});

	test("listPending returns only pending rows", async () => {
		const p1 = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: "cause-6",
				title: "active",
				summary: "",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "owner",
				autonomyDomain: "test",
				requiredLevel: 1,
			},
		);
		const p2 = await requestProposal(
			{ sql: pool.sql, bus },
			{
				origin: "owner_request",
				sourceCauseId: "cause-7",
				title: "rejected",
				summary: "",
				kind: "new_goal",
				payload: {},
				effectiveTrust: "owner",
				autonomyDomain: "test",
				requiredLevel: 1,
			},
		);
		await rejectProposal({ sql: pool.sql, bus }, p2.id, "user");

		const pending = await listPending(pool.sql);
		const ids = pending.map((p) => p.id);
		expect(ids).toContain(p1.id);
		expect(ids).not.toContain(p2.id);
	});
});

describe("consent ledger", () => {
	test("initial state: autonomous cloud egress disabled", async () => {
		const state = await readConsent(pool.sql);
		expect(state.autonomousCloudEgressEnabled).toBe(false);
	});

	test("grant flips to enabled and emits event", async () => {
		await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user", "bootstrap");
		const state = await readConsent(pool.sql);
		expect(state.autonomousCloudEgressEnabled).toBe(true);

		const rows = await pool.sql<{ type: string }[]>`
			SELECT type FROM events WHERE type = 'policy.autonomous_cloud_egress.enabled'
		`;
		expect(rows.length).toBeGreaterThan(0);
	});

	test("revoke flips back to disabled", async () => {
		await grantAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
		await revokeAutonomousCloudEgress({ sql: pool.sql, bus }, "user");
		const state = await readConsent(pool.sql);
		expect(state.autonomousCloudEgressEnabled).toBe(false);

		const rows = await pool.sql<{ type: string }[]>`
			SELECT type FROM events WHERE type = 'policy.autonomous_cloud_egress.disabled'
		`;
		expect(rows.length).toBeGreaterThan(0);
	});

	test("policy key matches the filter gate", async () => {
		// Sanity: `AUTONOMOUS_CLOUD_EGRESS` is the singleton key used by
		// both the grant path and the egress filter.
		expect(AUTONOMOUS_CLOUD_EGRESS).toBe("autonomous_cloud_egress");
	});
});

describe("degradation ladder", () => {
	test("initial read returns L0", async () => {
		const state = await readDegradation(pool.sql);
		expect(state.level).toBe(0);
	});

	test("setDegradation transitions to L2 and emits event", async () => {
		await setDegradation({ sql: pool.sql, bus }, 2, "budget_exceeded", "system");
		const state = await readDegradation(pool.sql);
		expect(state.level).toBe(2);
		expect(state.reason).toBe("budget_exceeded");
		const events = await pool.sql<{ data: { newLevel: number } }[]>`
			SELECT data FROM events WHERE type = 'degradation.level_changed'
		`;
		expect(events.length).toBe(1);
		expect(events[0]?.data.newLevel).toBe(2);
	});

	test("setting the same level is idempotent (no event)", async () => {
		await setDegradation({ sql: pool.sql, bus }, 0, "noop", "system");
		const events = await pool.sql<{ type: string }[]>`
			SELECT type FROM events WHERE type = 'degradation.level_changed'
		`;
		expect(events.length).toBe(0);
	});
});
