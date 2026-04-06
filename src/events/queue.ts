/**
 * HandlerQueue: per-handler serialized event processing queue.
 *
 * Each durable handler gets its own HandlerQueue that enforces:
 * - Events processed one at a time, in ULID order
 * - Replay events processed before live events (gated by replayDone flag)
 * - Monotonic checkpoint advancement (ULID dedup via comparison)
 * - Retry with dead-lettering after MAX_RETRIES failures
 *
 * The queue has two sub-queues: replay (populated during start()) and live
 * (populated by emit() after start). The drain loop consumes replay first,
 * then live. A wake mechanism avoids busy-waiting when both queues are empty.
 */

import type { Sql, TransactionSql } from "postgres";
import { advanceCursor, MAX_RETRIES, RETRY_DELAYS } from "./handlers.ts";
import type { EventId } from "./ids.ts";
import type { EventLog } from "./log.ts";
import type { Event } from "./types.ts";

/** Callback signature for durable handlers. */
type DurableHandler = (event: Event, tx: TransactionSql) => Promise<void>;

/**
 * Per-handler serialized queue with replay + live sub-queues.
 *
 * Lifecycle:
 * 1. Construct with handler info
 * 2. Call startDraining() to begin the drain loop (before replay begins)
 * 3. Enqueue replay events via enqueueReplay()
 * 4. Call replayComplete() to signal end of replay — only then will live events be processed
 * 5. Subsequent events arrive via enqueueLive()
 * 6. Call stop() during shutdown — finishes current event, does not drain
 * 7. Await drained() to wait for the drain loop to quiesce
 */
export class HandlerQueue {
	private replayQueue: Event[] = [];
	private replayIndex = 0;
	private liveQueue: Event[] = [];
	private draining = false;
	private processing = false;
	private stopped = false;
	private replayDone = false;
	private lastProcessedId: EventId | null = null;
	private wakeResolver: (() => void) | null = null;
	private drainedResolvers: (() => void)[] = [];
	private drainedPromise: Promise<void> | null = null;

	/** Enqueue an event from replay. Must be called before replayComplete(). */
	enqueueReplay(event: Event): void {
		this.replayQueue.push(event);
		this.wake();
	}

	// Events beyond this limit are dropped from the in-memory queue.
	// They are safely persisted in PostgreSQL and will be replayed from
	// the handler's checkpoint on next restart.
	private static readonly MAX_LIVE_QUEUE_SIZE = 10_000;

	/** Enqueue a live event (emitted after start()). */
	enqueueLive(event: Event): void {
		if (this.liveQueue.length >= HandlerQueue.MAX_LIVE_QUEUE_SIZE) {
			console.warn(`Live queue overflow — dropping event ${event.id} (safe in PostgreSQL)`);
			return;
		}
		this.liveQueue.push(event);
		this.wake();
	}

	/**
	 * Signal that replay is complete. Only after this call will the drain loop
	 * start processing events from the live queue.
	 */
	replayComplete(): void {
		this.replayDone = true;
		this.wake();
	}

	/** Start the drain loop. Must be called before enqueueReplay(). */
	startDraining(handler: DurableHandler, handlerId: string, sql: Sql, log: EventLog): void {
		if (this.draining) return;
		// Set draining synchronously so drained() callers see the correct state
		// immediately, not after the first microtask of drain().
		this.draining = true;
		this.drainedPromise = this.drain(handler, handlerId, sql, log);
	}

	/** Signal the drain loop to stop. It finishes the current event, then exits. */
	stop(): void {
		this.stopped = true;
		this.wake();
	}

	/**
	 * Returns a promise that resolves when the drain loop has quiesced
	 * (both queues empty and no event in flight) OR when the loop exits.
	 */
	drained(): Promise<void> {
		if (!this.draining) return Promise.resolve();
		if (this.drainedPromise !== null) {
			return new Promise<void>((resolve) => {
				this.drainedResolvers.push(resolve);
				// Wake the drain loop so it re-checks queue state
				this.wake();
			});
		}
		return Promise.resolve();
	}

	/** Wake the drain loop from its sleep. */
	private wake(): void {
		if (this.wakeResolver !== null) {
			const resolver = this.wakeResolver;
			this.wakeResolver = null;
			resolver();
		}
	}

	/**
	 * Signal quiescence if no more work and someone is waiting.
	 * Quiescence requires: replay is done, both queues empty, nothing processing.
	 */
	private signalQuiescence(): void {
		if (
			this.drainedResolvers.length > 0 &&
			this.replayDone &&
			!this.processing &&
			this.replayIndex >= this.replayQueue.length &&
			this.liveQueue.length === 0
		) {
			const resolvers = this.drainedResolvers.splice(0);
			for (const resolve of resolvers) {
				resolve();
			}
		}
	}

	/**
	 * Dequeue the next event to process.
	 * Replay events use an index cursor (O(1)) instead of Array.shift() (O(n))
	 * to avoid quadratic cost for large replay sets. Live events are only
	 * processed after replayComplete() has been called.
	 */
	private dequeue(): Event | undefined {
		if (this.replayIndex < this.replayQueue.length) {
			const event = this.replayQueue[this.replayIndex];
			this.replayIndex++;
			// Release consumed replay events to avoid holding references
			if (this.replayDone && this.replayIndex >= this.replayQueue.length) {
				this.replayQueue = [];
				this.replayIndex = 0;
			}
			return event;
		}
		if (this.replayDone) {
			return this.liveQueue.shift();
		}
		return undefined;
	}

	/** The main drain loop. Processes events one at a time in ULID order. */
	private async drain(
		handler: DurableHandler,
		handlerId: string,
		sql: Sql,
		log: EventLog,
	): Promise<void> {
		try {
			while (!this.stopped) {
				const event = this.dequeue();
				if (event === undefined) {
					this.signalQuiescence();
					// Sleep until woken by enqueue, replayComplete, or stop
					await new Promise<void>((resolve) => {
						this.wakeResolver = resolve;
					});
					continue;
				}

				// ULID dedup: skip events at or before the last processed ID
				if (this.lastProcessedId !== null && event.id <= this.lastProcessedId) {
					continue;
				}

				this.processing = true;
				try {
					// Inner retry loop: if even the dead-letter path fails (PG down),
					// retry the SAME event until it's handled or stop() is called.
					// The event is already dequeued — we must not lose it.
					let handled = false;
					while (!handled && !this.stopped) {
						handled = await this.executeWithRetry(handler, handlerId, event, sql, log);
						if (!handled) {
							await new Promise<void>((r) => {
								setTimeout(r, RETRY_DELAYS[RETRY_DELAYS.length - 1] ?? 2000);
							});
						}
					}
					if (handled) {
						this.lastProcessedId = event.id;
					}
				} finally {
					this.processing = false;
				}
			}
		} finally {
			this.draining = false;
			// Resolve all pending drained() waiters on exit
			const resolvers = this.drainedResolvers.splice(0);
			for (const resolve of resolvers) {
				resolve();
			}
		}
	}

	/**
	 * Execute a handler with retry logic and exponential backoff.
	 * After MAX_RETRIES failures, dead-letter the event.
	 *
	 * Returns true if the event was handled (success or dead-lettered).
	 * Returns false if even the dead-letter path failed (e.g., PG down) —
	 * the caller should NOT advance lastProcessedId so the event retries.
	 */
	private async executeWithRetry(
		handler: DurableHandler,
		handlerId: string,
		event: Event,
		sql: Sql,
		log: EventLog,
	): Promise<boolean> {
		for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
			try {
				await sql.begin(async (tx) => {
					await handler(event, tx);
					await advanceCursor(handlerId, event.id, tx);
				});
				return true;
			} catch (error: unknown) {
				if (attempt === MAX_RETRIES) {
					// Dead-letter: emit meta-event + advance cursor atomically.
					// Uses log.append() directly (not bus.emit()) to prevent cascading
					// failures — a handler for dead-letter events that itself fails would
					// create an infinite dead-letter loop. Dead-letter events are delivered
					// to handlers on next restart via replay.
					try {
						await sql.begin(async (tx) => {
							await log.append(
								{
									type: "system.handler.dead_lettered",
									version: 1,
									actor: "system",
									data: {
										handlerId,
										eventId: event.id,
										attempts: MAX_RETRIES,
										lastError: error instanceof Error ? error.message : String(error),
									},
									metadata: {},
								},
								tx,
							);
							await advanceCursor(handlerId, event.id, tx);
						});
						return true;
					} catch (dlError: unknown) {
						console.error(
							`Dead-letter failed for handler ${handlerId} on event ${event.id}:`,
							dlError,
						);
						return false;
					}
				}
				// Backoff before next retry attempt. Check stopped after sleep so
				// shutdown isn't delayed by backoff.
				await new Promise<void>((r) => {
					setTimeout(r, RETRY_DELAYS[attempt - 1] ?? 2000);
				});
				if (this.stopped) return false;
			}
		}
		return false;
	}
}
