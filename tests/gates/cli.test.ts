/**
 * Unit tests for the CLI gate.
 *
 * Tests are split into three layers:
 *   1. Pure helpers (commands matching, resolution) — no Ink, no React.
 *   2. Component render smoke tests via ink-testing-library — verify the
 *      TUI frames text correctly given props.
 *   3. Engine-wiring tests — mount <App/> with a stub ChatEngine and a
 *      real in-memory EventBus stub, drive via stdin writes, assert on
 *      engine call record + frames.
 *
 * Stubs replicate only the engine/bus surface the gate actually uses, so the
 * tests are fast and deterministic without the SDK subprocess, database, or
 * embeddings service.
 */

import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render } from "ink-testing-library";
import React from "react";
import type { ChatEngine } from "../../src/chat/engine.ts";
import type { TurnResult } from "../../src/chat/types.ts";
import type { EventBus } from "../../src/events/bus.ts";
import type { EphemeralEvent, Event } from "../../src/events/types.ts";
import { App } from "../../src/gates/cli/app.tsx";
import { matchSlashCommands, resolveSlashCommand } from "../../src/gates/cli/commands.ts";
import { CliGate } from "../../src/gates/cli/gate.ts";
import type { Gate } from "../../src/gates/types.ts";

// ---------------------------------------------------------------------------
// Ink-testing-library hygiene
// ---------------------------------------------------------------------------

afterEach(() => {
	cleanup();
});

// ---------------------------------------------------------------------------
// Stub engine + bus
// ---------------------------------------------------------------------------

interface EngineCall {
	readonly body: string;
	readonly gate: string;
}

interface StubEngine {
	readonly engine: ChatEngine;
	readonly calls: EngineCall[];
	readonly resetCalls: string[];
	readonly aborts: number;
	setNextResult(result: TurnResult): void;
	setNextRejection(error: Error): void;
	setHang(hang: boolean): void;
	releaseHang(): void;
}

function createStubEngine(): StubEngine {
	const calls: EngineCall[] = [];
	const resetCalls: string[] = [];
	const state = {
		nextResult: { ok: true as const, response: "ok" } as TurnResult,
		nextRejection: null as Error | null,
		hang: false,
		aborts: 0,
		releaseResolve: null as ((value: TurnResult) => void) | null,
	};

	const engine = {
		async handleMessage(body: string, gate: string): Promise<TurnResult> {
			calls.push({ body, gate });
			if (state.nextRejection !== null) {
				const err = state.nextRejection;
				state.nextRejection = null;
				throw err;
			}
			if (state.hang) {
				return await new Promise<TurnResult>((resolve) => {
					state.releaseResolve = resolve;
				});
			}
			return state.nextResult;
		},
		async resetSession(reason = "user_request"): Promise<void> {
			resetCalls.push(reason);
		},
		abortCurrentTurn(): void {
			state.aborts += 1;
			if (state.releaseResolve !== null) {
				state.releaseResolve({ ok: false, error: "interrupted" });
				state.releaseResolve = null;
			}
		},
	};

	return {
		engine: engine as unknown as ChatEngine,
		calls,
		resetCalls,
		get aborts() {
			return state.aborts;
		},
		setNextResult(result) {
			state.nextResult = result;
		},
		setNextRejection(error) {
			state.nextRejection = error;
		},
		setHang(hang) {
			state.hang = hang;
		},
		releaseHang() {
			if (state.releaseResolve !== null) {
				state.releaseResolve({ ok: true, response: "late" });
				state.releaseResolve = null;
			}
		},
	};
}

interface StubBus {
	readonly bus: EventBus;
	readonly emitted: Event[];
	emitEphemeral(event: EphemeralEvent): void;
	emitDurable(event: Event): void;
}

function createStubBus(): StubBus {
	const emitted: Event[] = [];
	const durableHandlers = new Map<string, ((event: Event) => Promise<void> | void)[]>();
	const ephemeralHandlers = new Map<EphemeralEvent["type"], ((event: EphemeralEvent) => void)[]>();

	const bus = {
		on<T extends Event["type"]>(
			type: T,
			handler: (event: Extract<Event, { type: T }>) => Promise<void> | void,
		): void {
			const list = durableHandlers.get(type) ?? [];
			list.push(handler as (event: Event) => Promise<void> | void);
			durableHandlers.set(type, list);
		},
		async emit(event: Omit<Event, "id" | "timestamp">): Promise<Event> {
			const full = {
				id: `ev-${String(emitted.length)}`,
				timestamp: new Date(),
				...event,
			} as unknown as Event;
			emitted.push(full);
			const handlers = durableHandlers.get(event.type);
			if (handlers !== undefined) {
				for (const h of handlers) {
					await h(full);
				}
			}
			return full;
		},
		emitEphemeral(event: EphemeralEvent): void {
			const handlers = ephemeralHandlers.get(event.type);
			if (handlers === undefined) return;
			for (const h of handlers) h(event);
		},
		onEphemeral<T extends EphemeralEvent["type"]>(
			type: T,
			handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
		): () => void {
			const list = ephemeralHandlers.get(type) ?? [];
			const typed = handler as (event: EphemeralEvent) => void;
			list.push(typed);
			ephemeralHandlers.set(type, list);
			return () => {
				const current = ephemeralHandlers.get(type);
				if (current === undefined) return;
				const idx = current.indexOf(typed);
				if (idx >= 0) current.splice(idx, 1);
			};
		},
		async start(): Promise<void> {},
		async stop(): Promise<void> {},
		async flush(): Promise<void> {},
	};

	return {
		bus: bus as unknown as EventBus,
		emitted,
		emitEphemeral(event) {
			bus.emitEphemeral(event);
		},
		emitDurable(event) {
			emitted.push(event);
			const handlers = durableHandlers.get(event.type);
			if (handlers === undefined) return;
			for (const h of handlers) void h(event);
		},
	};
}

// ---------------------------------------------------------------------------
// Ink test helpers
// ---------------------------------------------------------------------------

/** Wait a microtask / frame so Ink flushes pending state updates. */
async function flush(n = 2): Promise<void> {
	for (let i = 0; i < n; i++) {
		await Promise.resolve();
		await new Promise<void>((resolve) => {
			setTimeout(resolve, 0);
		});
	}
}

function renderApp(stubEngine: StubEngine, stubBus: StubBus, onExit: () => void = () => {}) {
	return render(
		React.createElement(App, {
			engine: stubEngine.engine,
			bus: stubBus.bus,
			onExit,
		}),
	);
}

// ---------------------------------------------------------------------------
// Layer 1: Pure command helpers
// ---------------------------------------------------------------------------

describe("slash command helpers", () => {
	test("matchSlashCommands returns empty list for non-slash prefix", () => {
		expect(matchSlashCommands("")).toEqual([]);
		expect(matchSlashCommands("hello")).toEqual([]);
	});

	test("matchSlashCommands filters by prefix on canonical names", () => {
		const matches = matchSlashCommands("/re");
		expect(matches.map((c) => c.name)).toContain("/reset");
	});

	test("matchSlashCommands also matches via aliases", () => {
		const matches = matchSlashCommands("/ex");
		expect(matches.map((c) => c.name)).toContain("/quit");
	});

	test("resolveSlashCommand returns canonical name for exact alias", () => {
		expect(resolveSlashCommand("/exit")).toBe("/quit");
		expect(resolveSlashCommand("/?")).toBe("/help");
	});

	test("resolveSlashCommand returns null for free-text input", () => {
		expect(resolveSlashCommand("hello world")).toBeNull();
		expect(resolveSlashCommand("/unknown")).toBeNull();
	});

	test("resolveSlashCommand ignores trailing arguments (first word only)", () => {
		expect(resolveSlashCommand("/reset now")).toBe("/reset");
	});
});

// ---------------------------------------------------------------------------
// Layer 2: CliGate structural contract
// ---------------------------------------------------------------------------

describe("CliGate", () => {
	test("implements Gate with name 'cli'", () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const gate: Gate = new CliGate(stubEngine.engine, stubBus.bus);
		expect(gate.name).toBe("cli");
		expect(typeof gate.start).toBe("function");
		expect(typeof gate.stop).toBe("function");
	});

	test("stop() is idempotent and does not throw when never started", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const gate = new CliGate(stubEngine.engine, stubBus.bus);
		await gate.stop();
		await gate.stop(); // second call is a no-op
	});
});

// ---------------------------------------------------------------------------
// Layer 3: App rendering + input driving
// ---------------------------------------------------------------------------

describe("App initial render", () => {
	test("renders the status bar in idle state", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { lastFrame } = renderApp(stubEngine, stubBus);
		await flush();
		const frame = lastFrame() ?? "";
		expect(frame).toContain("idle");
		expect(frame).toContain("you>");
	});

	test("shows the input prompt with a hint when idle and empty", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { lastFrame } = renderApp(stubEngine, stubBus);
		await flush();
		const frame = lastFrame() ?? "";
		expect(frame).toMatch(/Enter sends/i);
	});
});

describe("App message forwarding", () => {
	test("typing text and pressing Enter forwards to engine.handleMessage", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setNextResult({ ok: true, response: "hi" });
		const { stdin } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("hi there");
		await flush();
		stdin.write("\r");
		await flush(4);

		expect(stubEngine.calls).toHaveLength(1);
		expect(stubEngine.calls[0]).toEqual({ body: "hi there", gate: "cli" });
	});

	test("empty input submission does not call the engine", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("\r");
		await flush();

		expect(stubEngine.calls).toHaveLength(0);
	});

	test("engine returning ok=false shows the error inline, no crash", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setNextResult({ ok: false, error: "boom" });
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("hi");
		await flush();
		stdin.write("\r");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("error");
		expect(frame).toMatch(/boom/);
	});
});

describe("App slash commands", () => {
	test("/quit triggers onExit", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		let exited = false;
		const { stdin } = renderApp(stubEngine, stubBus, () => {
			exited = true;
		});
		await flush();

		stdin.write("/quit");
		await flush();
		stdin.write("\r");
		await flush();

		expect(exited).toBe(true);
	});

	test("/exit (alias) triggers onExit", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		let exited = false;
		const { stdin } = renderApp(stubEngine, stubBus, () => {
			exited = true;
		});
		await flush();

		stdin.write("/exit");
		await flush();
		stdin.write("\r");
		await flush();

		expect(exited).toBe(true);
	});

	test("/reset releases the session via engine.resetSession", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/reset");
		await flush();
		stdin.write("\r");
		await flush(4);

		expect(stubEngine.resetCalls).toHaveLength(1);
		expect(stubEngine.resetCalls[0]).toBe("user_request");
	});

	test("/clear does not call resetSession (message list only)", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/clear");
		await flush();
		stdin.write("\r");
		await flush();

		expect(stubEngine.resetCalls).toHaveLength(0);
		expect(stubEngine.calls).toHaveLength(0);
	});

	test("/help lists available commands inline", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/help");
		await flush();
		stdin.write("\r");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("/quit");
		expect(frame).toContain("/reset");
		expect(frame).toContain("/help");
	});

	test("/status appends the engine phase to the message list", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/status");
		await flush();
		stdin.write("\r");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toMatch(/phase:/i);
	});

	test("/memory appends a memory-stats line", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/memory");
		await flush();
		stdin.write("\r");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toMatch(/memory stats/i);
	});

	test("typing /re shows autocomplete popup with /reset", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/re");
		await flush();

		const frame = lastFrame() ?? "";
		expect(frame).toContain("/reset");
	});

	test("Tab after /re fills the input to /reset", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("/re");
		await flush();
		stdin.write("\t");
		await flush();

		const frame = lastFrame() ?? "";
		expect(frame).toContain("/reset");
	});
});

describe("App streaming display", () => {
	test("stream.chunk events render the text incrementally for the active session", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setHang(true);
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		// Pre-register the session so the TUI streamId filter matches.
		const sessionId = "sess_1";
		stubBus.emitDurable({
			id: "ev-s",
			type: "session.created",
			version: 1,
			timestamp: new Date(),
			actor: "system",
			data: { sessionId },
			metadata: { sessionId },
		} as unknown as Event);
		await flush();

		stdin.write("hi");
		await flush();
		stdin.write("\r");
		await flush(2);

		stubBus.emitEphemeral({
			type: "stream.chunk",
			data: { text: "Hel", sessionId },
		});
		stubBus.emitEphemeral({
			type: "stream.chunk",
			data: { text: "lo", sessionId },
		});
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("Hello");

		// Release the hanging turn so cleanup doesn't leak.
		stubEngine.releaseHang();
		await flush();
	});

	test("no stream.chunk events: the final response text is rendered from TurnResult", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setNextResult({ ok: true, response: "the final answer" });
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("hi");
		await flush();
		stdin.write("\r");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("the final answer");
	});
});

describe("App tool output display", () => {
	test("tool.start then tool.done renders name and duration", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setHang(true);
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		const sessionId = "sess_tool";
		stubBus.emitDurable({
			id: "ev-s",
			type: "session.created",
			version: 1,
			timestamp: new Date(),
			actor: "system",
			data: { sessionId },
			metadata: { sessionId },
		} as unknown as Event);
		await flush();

		stdin.write("hi");
		await flush();
		stdin.write("\r");
		await flush(2);

		// Force transition into streaming so tool calls attach to the message.
		stubBus.emitEphemeral({ type: "stream.chunk", data: { text: "x", sessionId } });
		await flush(2);
		stubBus.emitEphemeral({
			type: "tool.start",
			data: {
				callId: "call_1",
				name: "mcp__memory__store_memory",
				input: "{}",
				sessionId,
			},
		});
		await flush(4);

		let frame = lastFrame() ?? "";
		expect(frame).toMatch(/store_memory/);

		stubBus.emitEphemeral({
			type: "tool.done",
			data: { callId: "call_1", durationMs: 42, sessionId },
		});
		await flush(4);

		frame = lastFrame() ?? "";
		expect(frame).toContain("42ms");

		stubEngine.releaseHang();
		await flush();
	});
});

describe("App interrupt (Ctrl+C)", () => {
	test("during processing, Ctrl+C calls engine.abortCurrentTurn", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setHang(true);
		const { stdin } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("slow request");
		await flush();
		stdin.write("\r");
		await flush(2);

		// Ctrl+C (ETX = 0x03)
		stdin.write("\u0003");
		await flush(4);

		expect(stubEngine.aborts).toBeGreaterThanOrEqual(1);

		stubEngine.releaseHang();
		await flush();
	});

	test("when idle, Ctrl+C triggers onExit (quit)", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		let exited = false;
		const { stdin } = renderApp(stubEngine, stubBus, () => {
			exited = true;
		});
		await flush();

		stdin.write("\u0003");
		await flush();

		expect(exited).toBe(true);
	});

	test("after interrupt, the message list marks the turn as [interrupted]", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		stubEngine.setHang(true);
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("slow");
		await flush();
		stdin.write("\r");
		await flush(2);

		stdin.write("\u0003");
		await flush(4);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("[interrupted]");

		stubEngine.releaseHang();
		await flush();
	});
});

describe("App input history", () => {
	test("Up arrow after two submissions restores the most recent message", async () => {
		const stubEngine = createStubEngine();
		const stubBus = createStubBus();
		const { stdin, lastFrame } = renderApp(stubEngine, stubBus);
		await flush();

		stdin.write("first");
		await flush();
		stdin.write("\r");
		await flush(4);

		stdin.write("second");
		await flush();
		stdin.write("\r");
		await flush(4);

		// ANSI Up arrow (ESC [ A)
		stdin.write("\u001b[A");
		await flush(2);

		const frame = lastFrame() ?? "";
		expect(frame).toContain("second");
	});
});
