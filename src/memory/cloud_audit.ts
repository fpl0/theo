/**
 * Cloud egress auditing — `/cloud-audit` (`foundation.md §7.8`).
 *
 * Reads `cloud_egress.turn` events across a time window and produces a
 * per-class cost rollup. The CLI command projects onto this helper.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { TurnClass } from "../events/reflexes.ts";

export interface CloudAuditEntry {
	readonly turnClass: TurnClass;
	readonly turns: number;
	readonly costUsd: number;
	readonly inputTokens: number;
	readonly outputTokens: number;
}

export type AuditWindow = "day" | "week" | "month";

const WINDOW_MS: Record<AuditWindow, number> = {
	day: 24 * 60 * 60_000,
	week: 7 * 24 * 60 * 60_000,
	month: 30 * 24 * 60 * 60_000,
};

/**
 * Sum `cloud_egress.turn` spend across a window, grouped by turn class.
 * Returns every class observed in the window — absent classes are not
 * listed so callers can treat the result as a sparse audit.
 */
export async function auditCloudEgress(
	sql: Sql | TransactionSql,
	window: AuditWindow,
	at: Date = new Date(),
): Promise<readonly CloudAuditEntry[]> {
	const since = new Date(at.getTime() - WINDOW_MS[window]);
	const q = asQueryable(sql);
	const rows = await q<Record<string, unknown>[]>`
		SELECT
			data->>'turnClass'                      AS turn_class,
			COUNT(*)::text                          AS turns,
			COALESCE(SUM((data->>'costUsd')::numeric), 0)::text        AS cost,
			COALESCE(SUM((data->>'inputTokens')::bigint), 0)::text     AS input_tokens,
			COALESCE(SUM((data->>'outputTokens')::bigint), 0)::text    AS output_tokens
		FROM events
		WHERE type = 'cloud_egress.turn'
		  AND timestamp >= ${since}
		GROUP BY data->>'turnClass'
		ORDER BY data->>'turnClass'
	`;
	return rows.map((r) => ({
		turnClass: r["turn_class"] as TurnClass,
		turns: Number(r["turns"]),
		costUsd: Number(r["cost"]),
		inputTokens: Number(r["input_tokens"]),
		outputTokens: Number(r["output_tokens"]),
	}));
}
