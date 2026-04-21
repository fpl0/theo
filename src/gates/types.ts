/**
 * Shared types for Theo's gates.
 *
 * A gate is a boundary between Theo's core (chat engine + memory + scheduler)
 * and the outside world. The CLI, Telegram bot, HTTP API, and any future
 * interface all implement this interface.
 *
 * Gates are purely a presentation concern — they forward messages to the
 * engine and render responses. They never know about the memory layer,
 * database, or SDK internals.
 */

/** Minimal contract every gate implements. */
export interface Gate {
	/** Stable identifier carried on `message.received` events. */
	readonly name: string;

	/** Start the gate. Resolves when the gate has exited (user quit). */
	start(): Promise<void>;

	/** Stop the gate. Must be idempotent. Must NOT call `process.exit`. */
	stop(): Promise<void>;
}
