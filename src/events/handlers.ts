/**
 * Handler types and checkpoint management for event processing.
 *
 * Handlers are functions that process events. Durable handlers receive a
 * transaction for atomic side-effects + checkpoint writes. Ephemeral handlers
 * receive no transaction and are fire-and-forget.
 *
 * Retry/dead-letter execution logic lives in queue.ts alongside the drain loop.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventId } from "./ids.ts";
import type { Event } from "./types.ts";

/** Maximum retry attempts before dead-lettering an event. */
export const MAX_RETRIES = 3;

/** Exponential backoff delays (ms) between retry attempts. Index = attempt - 1. */
export const RETRY_DELAYS: readonly [number, number, number] = [100, 500, 2000];

/**
 * A durable handler receives a transaction for atomic side-effects + checkpoint.
 * An ephemeral handler receives no transaction.
 */
export type Handler<E extends Event = Event> = (event: E, tx?: TransactionSql) => Promise<void>;

/**
 * Handler mode — controls replay behavior.
 *
 * - `"decision"` (default): pure over event data. Runs on both live dispatch and
 *   replay. Use for projections, graph mutations, and anything that must rebuild
 *   from the event log.
 * - `"effect"`: calls the outside world (LLMs, git, network). Runs only on live
 *   dispatch; skipped during replay. The external result must be captured as a
 *   separate event so downstream decision handlers can observe it (the
 *   `*_requested` / `*_classified` pair pattern).
 *
 * See `foundation.md §7.4` for the rationale.
 */
export type HandlerMode = "decision" | "effect";

/** Options for registering a handler. Handlers with `id` are durable (checkpointed, replayed). */
export interface HandlerOptions {
	readonly id: string;
	/** Replay policy. Defaults to `"decision"`. */
	readonly mode?: HandlerMode;
}

/**
 * Advance a handler's checkpoint cursor atomically.
 * The WHERE guard ensures the cursor never regresses.
 */
export async function advanceCursor(
	handlerId: string,
	eventId: EventId,
	tx: TransactionSql,
): Promise<void> {
	const query = asQueryable(tx);
	await query`
		INSERT INTO handler_cursors (handler_id, cursor, updated_at)
		VALUES (${handlerId}, ${eventId}, now())
		ON CONFLICT (handler_id)
		DO UPDATE SET cursor = ${eventId}, updated_at = now()
		WHERE handler_cursors.cursor < ${eventId}
	`;
}

/**
 * Load a handler's checkpoint cursor from the database.
 * Returns null if the handler has no checkpoint yet.
 */
export async function getCursor(handlerId: string, sql: Sql): Promise<EventId | null> {
	const rows = await sql`
		SELECT cursor FROM handler_cursors WHERE handler_id = ${handlerId}
	`;
	const first = rows[0];
	if (first === undefined) return null;
	return first["cursor"] as EventId;
}
