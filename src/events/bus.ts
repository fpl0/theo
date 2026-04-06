/**
 * EventBus: unified durability and dispatch.
 *
 * Every emit() writes to the event log (PostgreSQL) first, then enqueues
 * to matching handler queues synchronously. emit() returns after the durable
 * write completes — handler processing is asynchronous.
 *
 * Durable handlers (registered with `id`) get checkpointed, replayed on start,
 * and retried on failure. Ephemeral handlers (no `id`) are fire-and-forget.
 *
 * The bus also supports ephemeral events (EphemeralEvent type) that dispatch
 * without hitting the database.
 */

import type { Sql, TransactionSql } from "postgres";
import type { Handler, HandlerOptions } from "./handlers.ts";
import { getCursor } from "./handlers.ts";
import type { EventLog } from "./log.ts";
import { HandlerQueue } from "./queue.ts";
import type { EphemeralEvent, Event } from "./types.ts";

// ---------------------------------------------------------------------------
// Shutdown timeout
// ---------------------------------------------------------------------------

const SHUTDOWN_TIMEOUT_MS = 30_000;

// ---------------------------------------------------------------------------
// Internal registration types
// ---------------------------------------------------------------------------

interface DurableRegistration {
	readonly type: Event["type"];
	readonly handler: Handler;
	readonly id: string;
	queue: HandlerQueue | null;
}

interface EphemeralRegistration {
	readonly type: Event["type"];
	readonly handler: Handler;
	readonly id: undefined;
}

type Registration = DurableRegistration | EphemeralRegistration;

interface EphemeralEventRegistration {
	readonly type: EphemeralEvent["type"];
	readonly handler: (event: EphemeralEvent) => void;
}

// ---------------------------------------------------------------------------
// EventBus interface
// ---------------------------------------------------------------------------

/** The EventBus: emit events, register handlers, start/stop lifecycle. */
export interface EventBus {
	/** Register a handler for events of the given type. */
	on<T extends Event["type"]>(
		type: T,
		handler: Handler<Extract<Event, { type: T }>>,
		options?: HandlerOptions,
	): void;

	/** Emit a durable event: write to log, then dispatch to handlers. */
	emit(
		event: Omit<Event, "id" | "timestamp">,
		options?: { readonly tx?: TransactionSql },
	): Promise<Event>;

	/** Emit an ephemeral event: dispatch without database persistence. */
	emitEphemeral(event: EphemeralEvent): void;

	/** Register a handler for ephemeral events. */
	onEphemeral<T extends EphemeralEvent["type"]>(
		type: T,
		handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
	): void;

	/** Start the bus: create partitions, replay durable handlers from checkpoints. */
	start(): Promise<void>;

	/** Stop the bus: finish current events, do not drain full queues. */
	stop(): Promise<void>;

	/** Drain all handler queues completely. Testing only. */
	flush(): Promise<void>;
}

// ---------------------------------------------------------------------------
// EventBus Implementation
// ---------------------------------------------------------------------------

/**
 * Create an EventBus backed by the given EventLog and postgres.js connection.
 */
export function createEventBus(log: EventLog, sql: Sql): EventBus {
	const handlers: Registration[] = [];
	const ephemeralHandlers: EphemeralEventRegistration[] = [];
	let started = false;

	function on<T extends Event["type"]>(
		type: T,
		handler: Handler<Extract<Event, { type: T }>>,
		options?: HandlerOptions,
	): void {
		// Cast Handler<Extract<Event, {type: T}>> to Handler<Event>. Safe because
		// enqueueToMatchingHandlers filters by registration.type before dispatch,
		// so the handler only receives events matching its registered type T.
		const registration: Registration =
			options?.id !== undefined
				? {
						type,
						handler: handler as Handler,
						id: options.id,
						queue: null,
					}
				: { type, handler: handler as Handler, id: undefined };

		handlers.push(registration);

		// Late registration: if bus is already started and this is durable, set up queue + replay
		if (started && registration.id !== undefined) {
			const durable = registration as DurableRegistration;
			setupDurableQueue(durable);
			// Background replay — live events are enqueued immediately via the queue.
			// Replay failures are logged but do not crash the process; the handler
			// misses historical events but will receive future live events.
			void replayHandler(durable).catch((error: unknown) => {
				console.error(`Late handler replay failed for ${durable.id}:`, error);
			});
		}
	}

	function setupDurableQueue(registration: DurableRegistration): void {
		const queue = new HandlerQueue();
		registration.queue = queue;
		// Cast Handler (tx optional) to DurableHandler (tx required). Safe because
		// the drain loop always provides a tx from sql.begin() — durable handlers
		// are never called without a transaction.
		queue.startDraining(
			registration.handler as (event: Event, tx: TransactionSql) => Promise<void>,
			registration.id,
			sql,
			log,
		);
	}

	function onEphemeral<T extends EphemeralEvent["type"]>(
		type: T,
		handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
	): void {
		ephemeralHandlers.push({ type, handler: handler as (event: EphemeralEvent) => void });
	}

	async function emit(
		event: Omit<Event, "id" | "timestamp">,
		options?: { readonly tx?: TransactionSql },
	): Promise<Event> {
		const persisted = await log.append(event, options?.tx);
		enqueueToMatchingHandlers(persisted);
		return persisted;
	}

	function emitEphemeral(event: EphemeralEvent): void {
		for (const registration of ephemeralHandlers) {
			if (registration.type !== event.type) continue;
			registration.handler(event);
		}
	}

	function enqueueToMatchingHandlers(event: Event): void {
		for (const registration of handlers) {
			if (registration.type !== event.type) continue;
			if (registration.id === undefined) {
				// Ephemeral handler: fire-and-forget, no retry. Errors logged for debugging.
				void registration.handler(event).catch((error: unknown) => {
					console.error(`Ephemeral handler failed for ${event.type}:`, error);
				});
				continue;
			}
			if (registration.queue !== null) {
				registration.queue.enqueueLive(event);
			}
		}
	}

	// Replay batch size — read this many events per query during replay.
	// Each batch is an independent SELECT (no long-lived transaction), so
	// PostgreSQL can vacuum between batches. ULID dedup in the queue handles
	// any overlap with live events arriving concurrently.
	const ReplayBatchSize = 1000;

	async function replayHandler(registration: DurableRegistration): Promise<void> {
		if (registration.queue === null) return;
		let cursor = await getCursor(registration.id, sql);

		while (true) {
			const batch: Event[] = [];
			const reader = cursor
				? log.readAfter(cursor, { types: [registration.type], limit: ReplayBatchSize })
				: log.read({ types: [registration.type], limit: ReplayBatchSize });
			for await (const event of reader) {
				batch.push(event);
			}
			if (batch.length === 0) break;
			for (const event of batch) {
				registration.queue?.enqueueReplay(event);
			}
			cursor = batch[batch.length - 1]?.id ?? cursor;
			if (batch.length < ReplayBatchSize) break;
		}

		registration.queue?.replayComplete();
	}

	async function start(): Promise<void> {
		// Step 1: Create partitions for current and next month (independent DDL, safe to parallelize)
		const now = new Date();
		await Promise.all([
			log.ensurePartition(now),
			log.ensurePartition(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1))),
		]);
		await log.loadKnownPartitions();

		// Step 2: Create handler queues and start drain loops BEFORE setting started = true
		const durableHandlers = handlers.filter((h): h is DurableRegistration => h.id !== undefined);
		for (const registration of durableHandlers) {
			setupDurableQueue(registration);
		}

		// Step 3: Set started = true BEFORE replay so live events are enqueued
		started = true;

		// Step 4: Replay each durable handler from checkpoint
		try {
			await Promise.all(durableHandlers.map(replayHandler));
		} catch (error) {
			// Replay failed — clean up queues so the bus is in a consistent stopped state.
			// The caller sees a clean failure and can retry start().
			await stop();
			throw error;
		}
	}

	/** Get all durable handlers that have active queues. */
	function activeQueues(): HandlerQueue[] {
		const queues: HandlerQueue[] = [];
		for (const h of handlers) {
			if (h.id !== undefined && h.queue !== null) {
				queues.push(h.queue);
			}
		}
		return queues;
	}

	async function stop(): Promise<void> {
		started = false;
		const queues = activeQueues();
		for (const queue of queues) {
			queue.stop();
		}
		let timeoutId: ReturnType<typeof setTimeout> | undefined;
		await Promise.race([
			Promise.allSettled(queues.map((q) => q.drained())),
			new Promise<void>((resolve) => {
				timeoutId = setTimeout(resolve, SHUTDOWN_TIMEOUT_MS);
			}),
		]);
		if (timeoutId !== undefined) clearTimeout(timeoutId);
	}

	async function flush(): Promise<void> {
		await Promise.all(activeQueues().map((q) => q.drained()));
	}

	return {
		on,
		onEphemeral,
		emit,
		emitEphemeral,
		start,
		stop,
		flush,
	};
}
