/**
 * Event log: append-only persistent store backed by PostgreSQL partitioned tables.
 *
 * The event log is Theo's primary record — the single source of truth from which
 * all other state is derived. Events are stored in monthly partitions (events_YYYY_MM).
 * Partitions are created lazily on write and proactively for current + next month.
 *
 * Reads apply upcasters lazily to transform old event schemas to current versions.
 */

import type postgres from "postgres";
import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import { computeEffectiveTrust } from "../memory/trust.ts";
import type { EventId } from "./ids.ts";
import { newEventId } from "./ids.ts";
import type { Event, EventMetadata } from "./types.ts";
import type { UpcasterRegistry } from "./upcasters.ts";

// ---------------------------------------------------------------------------
// Read Options
// ---------------------------------------------------------------------------

/** Options for reading events from the log. */
export interface ReadOptions {
	readonly types?: ReadonlyArray<Event["type"]>;
	readonly limit?: number;
	readonly tx?: TransactionSql;
}

/** Options for writing an event. */
export interface AppendOptions {
	readonly tx?: TransactionSql;
	/**
	 * Override effective trust for this event. Used by owner commands that
	 * promote a proposal-origin event (e.g., `/approve` elevating an
	 * ideation proposal to `owner` for the resulting `goal.confirmed`).
	 */
	readonly effectiveTrustOverride?: TrustTier;
	/**
	 * Seed tier — overrides `actorTrust(event.actor)` as the starting point
	 * of the min-walk. Used by the webhook gate to emit at `external`
	 * regardless of the actor field.
	 */
	readonly seedTier?: TrustTier;
}

// ---------------------------------------------------------------------------
// Partition Helpers
// ---------------------------------------------------------------------------

/** Compute the partition table name for a given timestamp. */
export function partitionName(timestamp: Date): string {
	const y = timestamp.getUTCFullYear();
	const m = String(timestamp.getUTCMonth() + 1).padStart(2, "0");
	return `events_${String(y)}_${m}`;
}

/** Compute the [from, to) bounds for the month containing timestamp. */
export function partitionBounds(timestamp: Date): { from: Date; to: Date } {
	const y = timestamp.getUTCFullYear();
	const m = timestamp.getUTCMonth();
	return {
		from: new Date(Date.UTC(y, m, 1)),
		to: new Date(Date.UTC(y, m + 1, 1)),
	};
}

// ---------------------------------------------------------------------------
// EventLog Interface
// ---------------------------------------------------------------------------

/** The EventLog: append events, read with upcasting, manage partitions. */
export interface EventLog {
	/**
	 * Append a partial event. Assigns ULID id + timestamp + effective trust
	 * (computed from the causation chain). Returns the complete event.
	 */
	append(
		event: Omit<Event, "id" | "timestamp">,
		options?: AppendOptions | TransactionSql,
	): Promise<Event>;

	/** Read all events in ULID order, applying upcasters lazily. */
	read(options?: ReadOptions): AsyncGenerator<Event>;

	/** Read events after a specific cursor in ULID order, applying upcasters. */
	readAfter(cursor: EventId, options?: ReadOptions): AsyncGenerator<Event>;

	/** Populate the known partitions set from pg_catalog. */
	loadKnownPartitions(): Promise<void>;

	/** Ensure a partition exists for the given timestamp. Idempotent. */
	ensurePartition(timestamp: Date): Promise<void>;
}

/**
 * Distinguish a bare `TransactionSql` handle from an `AppendOptions` record.
 * Tagged templates expose tagged-template invocation; plain objects do not.
 */
function isTransactionSql(value: AppendOptions | TransactionSql): value is TransactionSql {
	return typeof value === "function";
}

// ---------------------------------------------------------------------------
// EventLog Implementation
// ---------------------------------------------------------------------------

/**
 * Create an EventLog backed by the given postgres.js connection.
 * The upcaster registry is used to transform old event versions on read.
 */
export function createEventLog(sql: Sql, upcasters: UpcasterRegistry): EventLog {
	const knownPartitions = new Set<string>();

	async function loadKnownPartitions(): Promise<void> {
		const rows = await sql`
			SELECT c.relname AS name
			FROM pg_catalog.pg_inherits i
			JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
			JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
			WHERE p.relname = 'events'
		`;
		knownPartitions.clear();
		for (const row of rows) {
			knownPartitions.add(String(row["name"]));
		}
	}

	async function ensurePartition(timestamp: Date): Promise<void> {
		const name = partitionName(timestamp);
		if (knownPartitions.has(name)) return;

		const { from, to } = partitionBounds(timestamp);
		// Partition DDL does not support parameterized values (FOR VALUES FROM/TO
		// is DDL syntax, not DML). Values come from our own partitionBounds() —
		// ISO 8601 date strings, safe from injection.
		await sql.unsafe(
			`CREATE TABLE IF NOT EXISTS "${name}"
			 PARTITION OF events
			 FOR VALUES FROM ('${from.toISOString()}') TO ('${to.toISOString()}')`,
		);
		knownPartitions.add(name);
	}

	async function append(
		event: Omit<Event, "id" | "timestamp">,
		options?: AppendOptions | TransactionSql,
	): Promise<Event> {
		const id = newEventId();
		const timestamp = new Date();

		await ensurePartition(timestamp);

		// Backwards-compatible: tests call `log.append(event, tx)`. Detect a
		// transaction by checking for the tagged-template `sql` method the
		// caller will use; opts-object callers use the structured form.
		const opts: AppendOptions =
			options === undefined ? {} : isTransactionSql(options) ? { tx: options } : options;
		const tx = opts.tx;

		const effectiveTrust = await computeEffectiveTrust(tx ?? sql, event.actor, event.metadata, {
			...(opts.effectiveTrustOverride !== undefined
				? { override: opts.effectiveTrustOverride }
				: {}),
			...(opts.seedTier !== undefined ? { seedTier: opts.seedTier } : {}),
		});

		const query = asQueryable(tx ?? sql);
		await query`
			INSERT INTO events (id, type, version, timestamp, actor, data, metadata, effective_trust_tier)
			VALUES (
				${id},
				${event.type},
				${event.version},
				${timestamp},
				${event.actor},
				${sql.json(event.data as unknown as postgres.JSONValue)},
				${sql.json(event.metadata as unknown as postgres.JSONValue)},
				${effectiveTrust}
			)
		`;

		// The return type is Event (discriminated union). We reconstruct the event
		// from the input + assigned id/timestamp. The `as unknown as Event` is
		// provably safe: the caller provides a valid Omit<Event, "id"|"timestamp">
		// and we attach the missing fields.
		return {
			id,
			type: event.type,
			version: event.version,
			timestamp,
			actor: event.actor,
			data: event.data,
			metadata: event.metadata,
		} as unknown as Event;
	}

	/** Hydrate a database row into a typed Event, applying upcasters. */
	function hydrateRow(row: Record<string, unknown>): Event {
		const eventType = String(row["type"]);
		const storedVersion = Number(row["version"]);
		const rawData = row["data"] as Record<string, unknown>;
		const upcastedData = upcasters.upcast(eventType, storedVersion, rawData);
		const currentVersion = upcasters.currentVersions.get(eventType) ?? storedVersion;

		// Hydration from DB row to Event requires `as unknown as Event`.
		// This is provably safe: the row was written by append() with valid Event data.
		return {
			id: String(row["id"]) as EventId,
			type: eventType,
			version: currentVersion,
			timestamp: row["timestamp"] as Date,
			actor: String(row["actor"]),
			data: upcastedData,
			metadata: (row["metadata"] ?? {}) as EventMetadata,
		} as unknown as Event;
	}

	// Batch size for cursor-based streaming. Balances memory usage (one batch
	// in memory at a time) with round-trip overhead to PostgreSQL.
	const CursorBatchSize = 100;

	async function* read(options?: ReadOptions): AsyncGenerator<Event> {
		const query = asQueryable(options?.tx ?? sql);
		const types = options?.types;
		const limit = options?.limit;

		const typeFilter =
			types !== undefined && types.length > 0 ? sql`WHERE type = ANY(${types as string[]})` : sql``;
		const limitClause = limit !== undefined ? sql`LIMIT ${limit}` : sql``;

		const pending = query`
			SELECT id, type, version, timestamp, actor, data, metadata
			FROM events
			${typeFilter}
			ORDER BY id ASC
			${limitClause}
		`;

		for await (const batch of pending.cursor(CursorBatchSize)) {
			for (const row of batch) {
				yield hydrateRow(row as Record<string, unknown>);
			}
		}
	}

	async function* readAfter(cursorId: EventId, options?: ReadOptions): AsyncGenerator<Event> {
		const query = asQueryable(options?.tx ?? sql);
		const types = options?.types;
		const limit = options?.limit;

		const typeFilter =
			types !== undefined && types.length > 0 ? sql`AND type = ANY(${types as string[]})` : sql``;
		const limitClause = limit !== undefined ? sql`LIMIT ${limit}` : sql``;

		const pending = query`
			SELECT id, type, version, timestamp, actor, data, metadata
			FROM events
			WHERE id > ${cursorId}
			${typeFilter}
			ORDER BY id ASC
			${limitClause}
		`;

		for await (const batch of pending.cursor(CursorBatchSize)) {
			for (const row of batch) {
				yield hydrateRow(row as Record<string, unknown>);
			}
		}
	}

	return {
		append,
		read,
		readAfter,
		loadKnownPartitions,
		ensurePartition,
	};
}
