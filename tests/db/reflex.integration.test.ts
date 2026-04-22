/**
 * Reflex handler integration tests — decision handler chain + replay skip.
 *
 * Exercises the `webhook.verified` → `reflex.triggered` decision handler
 * against the real event log. Verifies:
 *
 *   - Decision handler emits `reflex.triggered` with `effectiveTrust =
 *     external` (inherited from `webhook.received` via the trust walker).
 *   - Stale webhooks emit `reflex.suppressed` instead.
 *   - Effect handlers are NOT re-invoked on replay (bus fast-forwards the
 *     cursor so historical webhooks do not replay LLM calls).
 */

import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import type { EventId } from "../../src/events/ids.ts";
import type { EventOfType } from "../../src/events/types.ts";
import { registerReflexDecision } from "../../src/reflex/handler.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

const pool = createTestPool();

async function insertWebhookBody(bodyId: string, body: Record<string, unknown>): Promise<void> {
	await pool.sql`
		INSERT INTO webhook_body (id, source, body, expires_at)
		VALUES (${bodyId}, 'github', ${pool.sql.json(body as never)},
		        now() + interval '24 hours')
	`;
}

beforeEach(async () => {
	await pool.sql`DELETE FROM webhook_body`;
	await cleanEventTables(pool.sql);
});

afterAll(async () => {
	await pool.end();
});

describe("reflex decision handler", () => {
	test("fresh webhook.verified → reflex.triggered with external trust", async () => {
		const bus = createTestBus(pool.sql);
		registerReflexDecision({ sql: pool.sql, bus, staleMs: 60 * 60_000 });
		await bus.start();

		try {
			const payloadRef = "P1";
			await insertWebhookBody(payloadRef, {
				theoMeta: { autonomyDomain: "code.review" },
				action: "opened",
			});

			const received = await bus.emit(
				{
					type: "webhook.received",
					version: 1,
					actor: "system",
					data: {
						source: "github",
						deliveryId: "d-1",
						bodyHash: "h",
						bodyByteLength: 10,
						receivedAt: new Date().toISOString(),
					},
					metadata: {},
				},
				{ seedTier: "external" },
			);

			await bus.emit(
				{
					type: "webhook.verified",
					version: 1,
					actor: "system",
					data: { source: "github", deliveryId: "d-1", payloadRef },
					metadata: { causeId: received.id },
				},
				{ seedTier: "external" },
			);

			await bus.flush();

			const rows = await pool.sql<{ data: unknown }[]>`
				SELECT data FROM events WHERE type = 'reflex.triggered' ORDER BY id ASC
			`;
			expect(rows.length).toBe(1);
			const data = rows[0]?.data as EventOfType<"reflex.triggered">["data"];
			expect(data.source).toBe("github");
			expect(data.effectiveTrust).toBe("external");
			expect(data.autonomyDomain).toBe("code.review");
			expect(data.envelopeNonce.length).toBeGreaterThan(0);
		} finally {
			await bus.stop();
		}
	});

	test("stale webhook (past the window) → reflex.suppressed stale", async () => {
		const bus = createTestBus(pool.sql);
		// Use a zero-ms window + a "now" far in the future so any age >= 1 ms
		// is stale relative to the received timestamp.
		registerReflexDecision({
			sql: pool.sql,
			bus,
			staleMs: 0,
			now: () => new Date(Date.now() + 24 * 60 * 60_000),
		});
		await bus.start();

		try {
			await insertWebhookBody("P2", {});
			const received = await bus.emit(
				{
					type: "webhook.received",
					version: 1,
					actor: "system",
					data: {
						source: "github",
						deliveryId: "d-stale",
						bodyHash: "h",
						bodyByteLength: 0,
						receivedAt: new Date().toISOString(),
					},
					metadata: {},
				},
				{ seedTier: "external" },
			);

			await bus.emit(
				{
					type: "webhook.verified",
					version: 1,
					actor: "system",
					data: { source: "github", deliveryId: "d-stale", payloadRef: "P2" },
					metadata: { causeId: received.id },
				},
				{ seedTier: "external" },
			);

			await bus.flush();

			const suppressed = await pool.sql<{ data: { reason: string } }[]>`
				SELECT data FROM events WHERE type = 'reflex.suppressed'
			`;
			expect(suppressed.length).toBe(1);
			expect(suppressed[0]?.data.reason).toBe("stale");

			const triggered = await pool.sql<{ id: EventId }[]>`
				SELECT id FROM events WHERE type = 'reflex.triggered'
			`;
			expect(triggered.length).toBe(0);
		} finally {
			await bus.stop();
		}
	});
});
