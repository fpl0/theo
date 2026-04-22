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
import type { TrustTier } from "../memory/graph/types.ts";
import type { Handler, HandlerMode, HandlerOptions } from "./handlers.ts";
import { getCursor } from "./handlers.ts";
import type { EventLog } from "./log.ts";
import { HandlerQueue } from "./queue.ts";
import type { EphemeralEvent, Event } from "./types.ts";

// ---------------------------------------------------------------------------
// Shutdown timeout
// ---------------------------------------------------------------------------

const SHUTDOWN_TIMEOUT_MS = 30_000;

/** Maximum passes `flush` will loop before reporting a non-converging cascade. */
const MAX_FLUSH_PASSES = 200;

// ---------------------------------------------------------------------------
// Internal registration types
// ---------------------------------------------------------------------------

interface DurableRegistration {
	readonly type: Event["type"];
	readonly handler: Handler;
	readonly id: string;
	/** Replay policy — `"effect"` handlers are skipped during replay. */
	readonly mode: HandlerMode;
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

/**
 * Options threaded through `bus.emit()` into `EventLog.append()`.
 *
 * `effectiveTrustOverride` is used by owner commands that elevate the
 * effective trust of a derived event (e.g., promoting an ideation proposal
 * to an `owner`-trust `goal.confirmed`). `seedTier` overrides the actor's
 * default starting tier for the causation walk — the webhook gate uses
 * this to force `external` regardless of actor.
 */
export interface EmitOptions {
	readonly tx?: TransactionSql;
	readonly effectiveTrustOverride?: TrustTier;
	readonly seedTier?: TrustTier;
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
	emit(event: Omit<Event, "id" | "timestamp">, options?: EmitOptions): Promise<Event>;

	/** Emit an ephemeral event: dispatch without database persistence. */
	emitEphemeral(event: EphemeralEvent): void;

	/** Register a handler for ephemeral events. Returns an unsubscribe function. */
	onEphemeral<T extends EphemeralEvent["type"]>(
		type: T,
		handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
	): () => void;

	/** Start the bus: create partitions, replay durable handlers from checkpoints. */
	start(): Promise<void>;

	/** Stop the bus: finish current events, do not drain full queues. */
	stop(): Promise<void>;

	/** Drain all handler queues completely. Testing only. */
	flush(): Promise<void>;

	/**
	 * Install (or replace) the durable-handler wrapper. Must be called BEFORE
	 * `start()` so the first handler registration sees the wrapper. No-op
	 * after the bus has been started.
	 */
	setDurableHandlerWrapper(wrapper: DurableHandlerWrapper): void;
}

// ---------------------------------------------------------------------------
// EventBus Implementation
// ---------------------------------------------------------------------------

/**
 * Optional hook for instrumenting durable handler dispatch. The telemetry
 * module supplies a wrapper that nests each handler invocation inside a
 * span and records duration/errors. When omitted, handlers run as-is —
 * the tests and legacy call paths don't require telemetry wiring.
 */
export type DurableHandlerWrapper = <E extends Event>(
	handlerId: string,
	mode: HandlerMode,
	handler: (event: E) => Promise<void>,
) => (event: E) => Promise<void>;

/**
 * `BusOptions.wrapHandler` resolves lazily, once at handler registration — the
 * top-level entrypoint creates the bus BEFORE the telemetry bundle (so the
 * projector can subscribe), then installs the wrapper via
 * `bus.setDurableHandlerWrapper`.
 */
export interface BusOptions {
	readonly wrapHandler?: DurableHandlerWrapper;
}

/**
 * Create an EventBus backed by the given EventLog and postgres.js connection.
 */
export function createEventBus(log: EventLog, sql: Sql, options: BusOptions = {}): EventBus {
	let wrapHandler: DurableHandlerWrapper | undefined = options.wrapHandler;
	const handlers: Registration[] = [];
	const ephemeralHandlers: EphemeralEventRegistration[] = [];
	let started = false;
	/**
	 * Monotonic counter bumped whenever a durable event is enqueued to a
	 * handler queue. `flush()` uses it to detect cascaded enqueues that
	 * happened after the initial `drained()` snapshot.
	 */
	let enqueueGeneration = 0;

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
						mode: options.mode ?? "decision",
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
		let dispatchHandler = registration.handler as (
			event: Event,
			tx: TransactionSql,
		) => Promise<void>;
		if (wrapHandler !== undefined) {
			// The bus-span wrapper produces a `(event) => Promise<void>` — it
			// doesn't know about transactions. We preserve the tx parameter by
			// binding the original handler and applying the wrapper around the
			// per-event invocation.
			const original = dispatchHandler;
			const wrapped = wrapHandler(registration.id, registration.mode, async (event: Event) => {
				// The transaction is provided at drain time — we intentionally
				// do NOT attempt to pass it through the wrapper. Keeping the
				// span tight around the transaction-scoped handler requires
				// threading `tx` through the wrapper signature, which would
				// force every test path to adopt the wrapper. Instead, the
				// wrapped handler receives the event only and re-invokes the
				// original with the tx captured from the drain-loop closure.
				// This works because the wrapper calls its inner function
				// synchronously with the event, so `currentTx` is always the
				// right one for this invocation.
				const tx = currentTx;
				if (tx === null) {
					throw new Error("durable handler invoked without a transaction");
				}
				await original(event, tx);
			});
			let currentTx: TransactionSql | null = null;
			dispatchHandler = async (event: Event, tx: TransactionSql): Promise<void> => {
				currentTx = tx;
				try {
					await wrapped(event);
				} finally {
					currentTx = null;
				}
			};
		}
		queue.startDraining(dispatchHandler, registration.id, sql, log);
	}

	function onEphemeral<T extends EphemeralEvent["type"]>(
		type: T,
		handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
	): () => void {
		const registration: EphemeralEventRegistration = {
			type,
			handler: handler as (event: EphemeralEvent) => void,
		};
		ephemeralHandlers.push(registration);
		return () => {
			const idx = ephemeralHandlers.indexOf(registration);
			if (idx >= 0) ephemeralHandlers.splice(idx, 1);
		};
	}

	async function emit(
		event: Omit<Event, "id" | "timestamp">,
		options?: EmitOptions,
	): Promise<Event> {
		// Build the log's AppendOptions only with fields that are actually
		// set — exactOptionalPropertyTypes forbids explicit-undefined spreads.
		const appendOptions: {
			tx?: TransactionSql;
			effectiveTrustOverride?: TrustTier;
			seedTier?: TrustTier;
		} = {};
		if (options?.tx !== undefined) appendOptions.tx = options.tx;
		if (options?.effectiveTrustOverride !== undefined) {
			appendOptions.effectiveTrustOverride = options.effectiveTrustOverride;
		}
		if (options?.seedTier !== undefined) appendOptions.seedTier = options.seedTier;
		const persisted = await log.append(event, appendOptions);
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
				enqueueGeneration++;
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

		// Effect handlers do NOT re-run on historical events. The outside world
		// has moved on — replaying an LLM classification or a network call
		// against a year-old event is wasteful and non-deterministic. Fast-
		// forward the cursor to the tail of the log so live events start from
		// the current point, then skip replay entirely.
		if (registration.mode === "effect") {
			await fastForwardCursor(registration.id, registration.type);
			registration.queue?.replayComplete();
			return;
		}

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

	/**
	 * For `effect` handlers: advance the cursor to the newest persisted event
	 * of the handler's type so future live events are processed from "now"
	 * forward. Historical events are not dispatched.
	 */
	async function fastForwardCursor(handlerId: string, type: Event["type"]): Promise<void> {
		const rows = await sql`
			SELECT id FROM events
			WHERE type = ${type}
			ORDER BY id DESC
			LIMIT 1
		`;
		const row = rows[0];
		if (row === undefined) return;
		const latestId = row["id"] as string;
		await sql`
			INSERT INTO handler_cursors (handler_id, cursor, updated_at)
			VALUES (${handlerId}, ${latestId}, now())
			ON CONFLICT (handler_id)
			DO UPDATE SET cursor = ${latestId}, updated_at = now()
			WHERE handler_cursors.cursor < ${latestId}
		`;
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
		// Cascading handlers (e.g., decision/effect chains) can enqueue new
		// events into a sibling queue AFTER that queue's `drained()` call
		// already resolved — the queue was empty when drained() fired, but
		// an upstream handler was still running and about to enqueue. We
		// cope by looping until the enqueue generation is stable across two
		// consecutive passes; a single pass can coincide with the moment
		// before an in-flight upstream enqueue lands.
		let lastGen = enqueueGeneration;
		let stableReadings = 0;
		for (let pass = 0; pass < MAX_FLUSH_PASSES; pass++) {
			await Promise.all(activeQueues().map((q) => q.drained()));
			// Yield the event loop so any pending microtasks / I/O complete.
			await new Promise<void>((resolve) => {
				setImmediate(resolve);
			});
			const gen = enqueueGeneration;
			if (gen === lastGen) {
				stableReadings++;
				if (stableReadings >= 2) return;
			} else {
				stableReadings = 0;
				lastGen = gen;
			}
		}
		throw new Error("flush: cascade did not converge after MAX_FLUSH_PASSES");
	}

	function setDurableHandlerWrapper(wrapper: DurableHandlerWrapper): void {
		if (started) return;
		wrapHandler = wrapper;
	}

	return {
		on,
		onEphemeral,
		emit,
		emitEphemeral,
		start,
		stop,
		flush,
		setDurableHandlerWrapper,
	};
}
