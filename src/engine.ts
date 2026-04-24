/**
 * Engine: Theo's lifecycle state machine.
 *
 * Sequences startup and shutdown across migrations, event bus replay, and the
 * gate. Pause/resume buffer incoming messages without dropping them — a
 * useful seam for graceful reloads.
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
 * Shutdown order is the reverse of startup: gate → bus → pool. A `stopping`
 * sentinel guards against double-stop from signal races (SIGTERM + SIGINT
 * arriving simultaneously or back-to-back).
 */

import type { ChatEngine } from "./chat/engine.ts";
import type { TurnResult } from "./chat/types.ts";
import { migrate } from "./db/migrate.ts";
import type { Pool } from "./db/pool.ts";
import { describeError } from "./errors.ts";
import type { EventBus } from "./events/bus.ts";
import type { Gate } from "./gates/types.ts";

/** The engine's lifecycle state. */
export type EngineState = "stopped" | "starting" | "running" | "paused" | "stopping";

interface QueuedMessage {
	readonly body: string;
	readonly gate: string;
	readonly resolve: (result: TurnResult) => void;
	readonly reject: (error: Error) => void;
}

export interface EngineDependencies {
	readonly pool: Pool;
	readonly bus: EventBus;
	readonly chatEngine: ChatEngine;
	readonly gate: Gate;
	/**
	 * Optional: semver/build identifier embedded in `system.started`. Reads
	 * from `package.json` would couple the engine to the filesystem; the
	 * caller passes it in.
	 */
	readonly version?: string;
}

export class Engine {
	private readonly deps: EngineDependencies;
	private readonly version: string;
	private stateValue: EngineState = "stopped";
	private readonly messageQueue: QueuedMessage[] = [];
	private stoppedResolvers: Array<() => void> = [];

	constructor(deps: EngineDependencies) {
		this.deps = deps;
		this.version = deps.version ?? "0.1.0";
	}

	get state(): EngineState {
		return this.stateValue;
	}

	get queuedMessageCount(): number {
		return this.messageQueue.length;
	}

	awaitStopped(): Promise<void> {
		if (this.stateValue === "stopped") return Promise.resolve();
		return new Promise<void>((resolve) => {
			this.stoppedResolvers.push(resolve);
		});
	}

	async start(): Promise<void> {
		if (this.stateValue !== "stopped") {
			throw new Error(`Engine.start(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "starting";

		try {
			const migrateResult = await migrate(this.deps.pool.sql);
			if (!migrateResult.ok) {
				throw migrateResult.error;
			}

			await this.deps.bus.start();

			await this.deps.bus.emit({
				type: "system.started",
				version: 1,
				actor: "system",
				data: { version: this.version },
				metadata: {},
			});

			void this.runGate();

			this.stateValue = "running";
		} catch (error) {
			this.stateValue = "stopped";
			throw error;
		}
	}

	async stop(reason: string): Promise<void> {
		if (this.stateValue === "stopping" || this.stateValue === "stopped") return;
		this.stateValue = "stopping";

		this.drainQueueWithError(new Error("engine stopped"));

		await safeShutdown("gate.stop", () => this.deps.gate.stop());

		await this.deps.bus.emit({
			type: "system.stopped",
			version: 1,
			actor: "system",
			data: { reason },
			metadata: {},
		});

		await safeShutdown("bus.stop", () => this.deps.bus.stop());
		await safeShutdown("pool.end", () => this.deps.pool.end());
		this.stateValue = "stopped";
		const resolvers = this.stoppedResolvers;
		this.stoppedResolvers = [];
		for (const resolve of resolvers) resolve();
	}

	pause(): void {
		if (this.stateValue !== "running") {
			throw new Error(`Engine.pause(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "paused";
	}

	async resume(): Promise<void> {
		if (this.stateValue !== "paused") {
			throw new Error(`Engine.resume(): invalid state ${this.stateValue}`);
		}
		this.stateValue = "running";
		await this.drainQueue();
	}

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

	private async runGate(): Promise<void> {
		try {
			await this.deps.gate.start();
		} catch (error) {
			console.error(`Gate exited with error: ${describeError(error)}`);
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
}

async function safeShutdown(label: string, fn: () => Promise<void>): Promise<void> {
	try {
		await fn();
	} catch (error) {
		console.error(`${label} failed: ${describeError(error)}`);
	}
}

/**
 * Install SIGINT/SIGTERM handlers that call `engine.stop(reason)` once.
 * Returns an uninstaller for tests.
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
