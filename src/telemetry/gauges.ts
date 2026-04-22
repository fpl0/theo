/**
 * Observable gauges — the only place periodic DB-backed metric pulls live.
 *
 * These callbacks run on every collection tick (or on `meter.collect()` in
 * tests). Each touches the DB once; failures are swallowed so a transient
 * DB blip never crashes the meter.
 */

import type { Sql } from "postgres";
import { readDegradation } from "../degradation/state.ts";
import type { MetricsRegistry } from "./metrics.ts";

export interface GaugeDeps {
	readonly metrics: MetricsRegistry;
	readonly sql: Sql;
}

/** Wire every DB-backed gauge. Called once from `initTelemetry`. */
export function registerGauges(deps: GaugeDeps): void {
	const { metrics, sql } = deps;

	metrics.nodesGauge.addCallback(async () => {
		try {
			const rows = await sql<{ count: number }[]>`SELECT count(*)::int AS count FROM node`;
			metrics.nodesGauge.observe(rows[0]?.count ?? 0);
		} catch {
			// swallow — gauge observability must not block
		}
	});

	metrics.embeddingBytesGauge.addCallback(async () => {
		try {
			const rows = await sql<{ bytes: string }[]>`
				SELECT COALESCE(SUM(pg_column_size(embedding)), 0)::text AS bytes FROM node WHERE embedding IS NOT NULL
			`;
			metrics.embeddingBytesGauge.observe(Number(rows[0]?.bytes ?? 0));
		} catch {
			// swallow
		}
	});

	metrics.goalsActive.addCallback(async () => {
		try {
			const rows = await sql<{ count: number }[]>`
				SELECT count(*)::int AS count FROM goal WHERE state = 'active'
			`;
			metrics.goalsActive.observe(rows[0]?.count ?? 0);
		} catch {
			// swallow
		}
	});

	metrics.goalsQuarantined.addCallback(async () => {
		try {
			const rows = await sql<{ count: number }[]>`
				SELECT count(*)::int AS count FROM goal WHERE state = 'quarantined'
			`;
			metrics.goalsQuarantined.observe(rows[0]?.count ?? 0);
		} catch {
			// swallow
		}
	});

	metrics.proposalsPending.addCallback(async () => {
		try {
			const rows = await sql<{ count: number }[]>`
				SELECT count(*)::int AS count FROM proposal WHERE status = 'pending'
			`;
			metrics.proposalsPending.observe(rows[0]?.count ?? 0);
		} catch {
			// swallow
		}
	});

	metrics.handlerLag.addCallback(async () => {
		try {
			const rows = await sql<{ handler: string; lag: string }[]>`
				SELECT handler_id AS handler, EXTRACT(EPOCH FROM now() - updated_at)::text AS lag
				FROM handler_cursors
			`;
			for (const row of rows) metrics.handlerLag.observe(Number(row.lag), { handler: row.handler });
		} catch {
			// swallow
		}
	});

	metrics.degradationLevel.addCallback(async () => {
		try {
			const state = await readDegradation(sql);
			metrics.degradationLevel.observe(state.level);
		} catch {
			// swallow
		}
	});

	// Process-level gauges are driven by Bun / Node APIs, not DB queries, but
	// live here for colocation with the other collectors.
	metrics.processMemoryRss.addCallback(() => {
		metrics.processMemoryRss.observe(process.memoryUsage().rss);
	});

	metrics.processEventLoopLag.addCallback(() => {
		// Simple approximation: sample the time between setImmediate dispatches.
		const start = performance.now();
		setImmediate(() => {
			const lag = performance.now() - start;
			metrics.processEventLoopLag.observe(lag);
		});
	});
}
