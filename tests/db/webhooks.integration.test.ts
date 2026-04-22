/**
 * Webhook gate integration tests — secrets, rate limiter, delivery dedup.
 *
 * Exercises the persisted state: `webhook_secret` rotation grace window,
 * token-bucket consume semantics, and `webhook_delivery` dedup PK.
 */

import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import {
	createRateLimiter,
	DEFAULT_CAPACITY,
	DEFAULT_REFILL_RATE,
} from "../../src/gates/webhooks/rate_limit.ts";
import { createWebhookSecretStore } from "../../src/gates/webhooks/secrets.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

const pool = createTestPool();
const bus = createTestBus(pool.sql);

beforeEach(async () => {
	await pool.sql`DELETE FROM webhook_secret`;
	await pool.sql`DELETE FROM webhook_delivery`;
	await pool.sql`DELETE FROM reflex_rate_limit`;
	await cleanEventTables(pool.sql);
});

afterAll(async () => {
	await pool.end();
});

describe("WebhookSecretStore", () => {
	test("register creates a secret and emits a rotation event", async () => {
		const store = createWebhookSecretStore(pool.sql, bus);
		const secret = await store.register("github", "user");
		expect(secret.length).toBeGreaterThan(0);
		const pair = await store.getSecrets("github");
		expect(pair?.current).toBe(secret);
		expect(pair?.previous).toBeNull();

		const events = await pool.sql<{ data: { source: string } }[]>`
			SELECT data FROM events WHERE type = 'webhook.secret_rotated'
		`;
		expect(events[0]?.data.source).toBe("github");
	});

	test("rotate records previous within grace window", async () => {
		const store = createWebhookSecretStore(pool.sql, bus, { graceMs: 60_000 });
		const first = await store.register("linear", "user");
		const second = await store.rotate("linear", "user");
		const pair = await store.getSecrets("linear");
		expect(pair?.current).toBe(second);
		expect(pair?.previous).toBe(first);
	});

	test("after grace window, previous is not returned", async () => {
		const store = createWebhookSecretStore(pool.sql, bus, {
			graceMs: 1000, // set a 1-second window for this test only
			now: () => new Date(),
		});
		const first = await store.register("github", "user");
		const second = await store.rotate("github", "user");
		expect(first).not.toBe(second);

		// Manually age the grace expiry by setting the column to the past.
		await pool.sql`
			UPDATE webhook_secret
			SET secret_previous_expires_at = now() - interval '2 seconds'
			WHERE source = 'github'
		`;
		const pair = await store.getSecrets("github");
		expect(pair?.current).toBe(second);
		expect(pair?.previous).toBeNull();
	});

	test("sweepExpired clears expired previous + emits grace expired event", async () => {
		const store = createWebhookSecretStore(pool.sql, bus, { graceMs: 1000 });
		await store.register("github", "user");
		await store.rotate("github", "user");
		await pool.sql`
			UPDATE webhook_secret
			SET secret_previous_expires_at = now() - interval '1 second'
			WHERE source = 'github'
		`;
		const swept = await store.sweepExpired(new Date());
		expect(swept).toContain("github");
		const events = await pool.sql<{ data: { source: string } }[]>`
			SELECT data FROM events WHERE type = 'webhook.secret_grace_expired'
		`;
		expect(events[0]?.data.source).toBe("github");
	});
});

describe("createRateLimiter", () => {
	test("fresh source is seeded with capacity - 1 tokens and allowed", async () => {
		const limiter = createRateLimiter(pool.sql);
		const decision = await limiter.consume("github");
		expect(decision.allowed).toBe(true);
	});

	test("burst exceeds capacity → returns 429 with Retry-After", async () => {
		const limiter = createRateLimiter(pool.sql, {
			defaultPolicy: { capacity: 3, refillRate: 0.0001 },
		});
		// Exhaust capacity
		for (let i = 0; i < 3; i++) {
			const d = await limiter.consume("github");
			expect(d.allowed).toBe(true);
		}
		const decision = await limiter.consume("github");
		expect(decision.allowed).toBe(false);
		if (!decision.allowed) {
			expect(decision.retryAfterSec).toBeGreaterThanOrEqual(1);
		}
	});

	test("per-source isolation: github burst does not throttle linear", async () => {
		const limiter = createRateLimiter(pool.sql, {
			defaultPolicy: { capacity: 2, refillRate: 0.0001 },
		});
		for (let i = 0; i < 2; i++) await limiter.consume("github");
		const blockedGithub = await limiter.consume("github");
		const allowedLinear = await limiter.consume("linear");
		expect(blockedGithub.allowed).toBe(false);
		expect(allowedLinear.allowed).toBe(true);
	});

	test("reset clears the bucket for a source", async () => {
		const limiter = createRateLimiter(pool.sql, {
			defaultPolicy: { capacity: 1, refillRate: 0.0001 },
		});
		await limiter.consume("github");
		const blocked = await limiter.consume("github");
		expect(blocked.allowed).toBe(false);
		await limiter.reset("github");
		const afterReset = await limiter.consume("github");
		expect(afterReset.allowed).toBe(true);
	});

	test("defaults match plan (60 req/min, burst 10)", () => {
		expect(DEFAULT_CAPACITY).toBe(10);
		expect(DEFAULT_REFILL_RATE).toBe(1);
	});
});
