/**
 * CliGate: the boundary between Theo's core and a terminal session.
 *
 * Implements the Gate interface by mounting the Ink application. `start()`
 * blocks until the user quits (or `stop()` is called from outside). `stop()`
 * unmounts Ink, which restores the terminal to its original state. It does
 * NOT call `process.exit` — Phase 14 (engine lifecycle) owns shutdown.
 *
 * The `render` import is dynamic so importing the gate's module graph doesn't
 * pull Ink (and React) into processes that don't need a TUI, such as the
 * scheduler or background-intelligence workers.
 */

import React from "react";
import type { ChatEngine } from "../../chat/engine.ts";
import type { EventBus } from "../../events/bus.ts";
import type { Gate } from "../types.ts";
import { App } from "./app.tsx";

interface InkInstance {
	readonly waitUntilExit: () => Promise<unknown>;
	readonly unmount: () => void;
}

export class CliGate implements Gate {
	readonly name = "cli";
	private instance: InkInstance | null = null;
	private stopped = false;

	constructor(
		private readonly engine: ChatEngine,
		private readonly bus: EventBus,
	) {}

	async start(): Promise<void> {
		if (this.stopped) return;
		if (process.stdin.isTTY !== true || process.stdout.isTTY !== true) {
			throw new Error(
				"CLI gate requires an interactive TTY (stdin and stdout). " +
					"Run `bun run src/index.ts` directly in a terminal, or use a " +
					"non-CLI gate for non-TTY deployments.",
			);
		}
		const { render } = await import("ink");
		const handleExit = (): void => {
			this.instance?.unmount();
		};
		const instance: InkInstance = render(
			React.createElement(App, {
				engine: this.engine,
				bus: this.bus,
				onExit: handleExit,
			}),
		);
		this.instance = instance;
		await instance.waitUntilExit();
	}

	async stop(): Promise<void> {
		if (this.stopped) return;
		this.stopped = true;
		this.instance?.unmount();
		// `unmount` is synchronous; await a microtask so any pending Ink cleanup
		// flushes before the caller continues.
		await Promise.resolve();
	}
}
