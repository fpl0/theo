/**
 * Engine: Theo's lifecycle state machine.
 *
 * Sequences startup and shutdown across migrations, event bus replay, the
 * scheduler, gates, and the chat engine. Pause/resume buffer incoming
 * messages without dropping them — a useful seam for graceful reloads.
 *
 * State diagram:
 *
 *   stopped ──start()──▶ starting ──▶ running ◀──pause()/resume()──▶ paused
 *                                        │                              │
 *                                        └─────stop()────▶ stopping ◀───┘
 *                                                              │
 *                                                              ▼
 *                                                           stopped
 *
 * Shutdown order is the reverse of startup: gate → scheduler → bus → pool.
 * A `stopping` sentinel guards against double-stop from signal races
 * (SIGTERM + SIGINT arriving simultaneously or back-to-back).
 */

import type { ChatEngine } from "./chat/engine.ts";
import type { TurnResult } from "./chat/types.ts";
import { migrate } from "./db/migrate.ts";
import type { Pool } from "./db/pool.ts";
import type { EventBus } from "./events/bus.ts";
import type { Gate } from "./gates/types.ts";
import type { Scheduler } from "./scheduler/runner.ts";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/** The engine's lifecycle state. */
export type EngineState = "stopped" | "starting" | "running" | "paused" | "stopping";

/** A message parked in the pause queue. */
interface QueuedMessage {
	readonly body: string;
	readonly gate: string;
	readonly resolve: (result: TurnResult) => void;
	readonly reject: (error: Error) => void;
}

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

/**
 * Everything the engine orchestrates at startup. `pool` is the database
 * pool wrapper (so the engine can call `.end()` on shutdown without
 * reaching for the raw `postgres.Sql`); `chatEngine` processes messages;
 * `gate` is the active interactive surface (currently only one; Phase 13b
 * adds webhook gates).
 */
export interface EngineDependencies {
	readonly pool: Pool;
	readonly bus: EventBus;
	readonly scheduler: Scheduler;
	readonly chatEngine: ChatEngine;
	readonly gate: Gate;
	/**
	 * Optional: semver/build identifier embedded in `system.started`. Reads
	 * from `package.json` would couple the engine to the filesystem; the
	 * caller passes it in.
	 */
	readonly version?: string;
}

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

export class Engine {
	private readonly deps: EngineDependencies;
	private readonly version: string;
	private stateValue: EngineState = "stopped";
	/**
	 * Reentrancy guard set when `stop()` begins. Prevents double-stop when
	 * both SIGTERM and SIGINT arrive, or when `stop()` is invoked during an
	 * already-in-progress shutdown.
	 */
	private stopping = false;
	private readonly messageQueue: QueuedMessage[] = [];

	constructor(deps: EngineDependencies) {
		this.deps = deps;
		this.version = deps.version ?? "0.1.0";
	}

	/** Current state. Surfaced for status displays and tests. */
	get state(): EngineState {
		return this.stateValue;
	}

	/** Number of messages currently parked in the pause queue. */
	get queuedMessageCount(): number {
		return this.messageQueue.length;
	}

	/**
	 * Full startup sequence. Runs migrations, starts the event bus (which
	 * replays durable handler checkpoints), starts the scheduler, emits
	 * `system.started`, then starts the gate. Any failure rolls the state
	 * back to `stopped` and propagates.
	 */
	async start(): Promise<void> {
		if (this.stateValue !== "stopped") {
			throw new Error(`Engine.start(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "starting";
		this.stopping = false;

		try {
			const migrateResult = await migrate(this.deps.pool.sql);
			if (!migrateResult.ok) {
				throw migrateResult.error;
			}

			await this.deps.bus.start();
			await this.deps.scheduler.start();

			await this.deps.bus.emit({
				type: "system.started",
				version: 1,
				actor: "system",
				data: { version: this.version },
				metadata: {},
			});

			// Start the gate last — messages can only arrive once we are ready
			// to service them. The gate's `start()` may block until the user
			// quits (e.g., the CLI TUI); callers typically don't `await` it
			// synchronously.
			void this.runGate();

			this.stateValue = "running";
		} catch (error) {
			// Put the engine back in a stopped state so `start()` can be
			// retried once the caller has fixed the underlying issue.
			this.stateValue = "stopped";
			throw error;
		}
	}

	/**
	 * Full shutdown sequence in reverse dependency order:
	 *   gate → scheduler → bus → pool. Emits `system.stopped` AFTER the
	 * scheduler quiesces but BEFORE the bus stops, so the event is durably
	 * written. Idempotent via `stopping`.
	 */
	async stop(reason: string): Promise<void> {
		// Guard against re-entry from signal races AND post-shutdown re-calls.
		// Once state is `stopped`, a subsequent stop() must be a no-op so pool
		// end and bus emits don't fire twice.
		if (this.stopping || this.stateValue === "stopped") return;
		this.stopping = true;

		this.stateValue = "stopping";

		try {
			// Reject any messages still parked in the pause queue before
			// shutting down — the caller awaits their promises and needs to
			// unblock.
			this.drainQueueWithError(new Error("engine stopped"));

			await this.safeStopGate();
			await this.safeStopScheduler();

			await this.deps.bus.emit({
				type: "system.stopped",
				version: 1,
				actor: "system",
				data: { reason },
				metadata: {},
			});

			await this.safeStopBus();
			await this.safeEndPool();
			this.stateValue = "stopped";
		} finally {
			// Leave `stopping` true only while the async teardown is in flight;
			// once resolved, future start() calls reset it.
			this.stopping = false;
		}
	}

	/** Transition to `paused`. Incoming messages are queued until resume. */
	pause(): void {
		if (this.stateValue !== "running") {
			throw new Error(`Engine.pause(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "paused";
	}

	/**
	 * Transition from `paused` back to `running` and drain any queued
	 * messages in arrival order. Drain is sequential so session state (one
	 * turn at a time) is preserved.
	 */
	async resume(): Promise<void> {
		if (this.stateValue !== "paused") {
			throw new Error(`Engine.resume(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "running";
		await this.drainQueue();
	}

	/**
	 * Entry point for gates. If the engine is running, forward to the chat
	 * engine immediately; if paused, park the message until `resume()`.
	 * Throws when stopped or stopping — the gate should stop accepting
	 * input before the engine reaches those states.
	 */
	async handleMessage(body: string, gate: string): Promise<TurnResult> {
		if (this.stateValue === "running") {
			return this.deps.chatEngine.handleMessage(body, gate);
		}
		if (this.stateValue === "paused") {
			return new Promise<TurnResult>((resolve, reject) => {
				this.messageQueue.push({ body, gate, resolve, reject });
			});
		}
		throw new Error(`Engine.handleMessage(): invalid state ${this.stateValue}`);
	}

	// -------------------------------------------------------------------------
	// Internal helpers
	// -------------------------------------------------------------------------

	/**
	 * Run the gate. Kept in a separate method so `start()` can fire-and-forget
	 * without swallowing the gate's eventual exit. Errors from the gate are
	 * logged; signal handlers own the subsequent `stop()` call.
	 */
	private async runGate(): Promise<void> {
		try {
			await this.deps.gate.start();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`Gate exited with error: ${message}`);
		}
	}

	private async drainQueue(): Promise<void> {
		while (this.messageQueue.length > 0) {
			if (this.stateValue !== "running") break;
			const msg = this.messageQueue.shift();
			if (msg === undefined) break;
			try {
				const result = await this.deps.chatEngine.handleMessage(msg.body, msg.gate);
				msg.resolve(result);
			} catch (error) {
				msg.reject(error instanceof Error ? error : new Error(String(error)));
			}
		}
	}

	private drainQueueWithError(error: Error): void {
		while (this.messageQueue.length > 0) {
			const msg = this.messageQueue.shift();
			if (msg === undefined) break;
			msg.reject(error);
		}
	}

	private async safeStopGate(): Promise<void> {
		try {
			await this.deps.gate.stop();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`Gate stop failed: ${message}`);
		}
	}

	private async safeStopScheduler(): Promise<void> {
		try {
			await this.deps.scheduler.stop();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`Scheduler stop failed: ${message}`);
		}
	}

	private async safeStopBus(): Promise<void> {
		try {
			await this.deps.bus.stop();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`Bus stop failed: ${message}`);
		}
	}

	private async safeEndPool(): Promise<void> {
		try {
			await this.deps.pool.end();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`Pool shutdown failed: ${message}`);
		}
	}
}

// ---------------------------------------------------------------------------
// Signal installer
// ---------------------------------------------------------------------------

/**
 * Install SIGINT/SIGTERM handlers that call `engine.stop(reason)` once.
 * Returns an uninstaller for tests. Safe to call multiple times — the
 * engine's `stopping` flag keeps concurrent signals idempotent.
 */
export function installSignalHandlers(engine: Engine): () => void {
	const onSigterm = (): void => {
		void engine.stop("SIGTERM");
	};
	const onSigint = (): void => {
		void engine.stop("SIGINT");
	};
	process.on("SIGTERM", onSigterm);
	process.on("SIGINT", onSigint);
	return () => {
		process.off("SIGTERM", onSigterm);
		process.off("SIGINT", onSigint);
	};
}
