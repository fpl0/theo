/**
 * Unit tests for ChatEngine.handleMessage().
 *
 * The engine is the orchestrator for a single turn:
 *   message.received → session decide/start → prompt assembly →
 *   SDK query() → stream chunks → turn.completed / turn.failed.
 *
 * Tests inject a `queryFn` seam that yields canned SDK messages, plus stub
 * repositories and a counting EventBus. No subprocess, no database, no
 * network — pure Bun unit tests.
 */

import { describe, expect, test } from "bun:test";
import type {
	McpSdkServerConfigWithInstance,
	Options,
	Query,
	SDKMessage,
} from "@anthropic-ai/claude-agent-sdk";
import type { ContextDependencies } from "../../src/chat/context.ts";
import { ChatEngine } from "../../src/chat/engine.ts";
import { SessionManager } from "../../src/chat/session.ts";
import { ok, type Result } from "../../src/errors.ts";
import type { EventBus } from "../../src/events/bus.ts";
import type { Event, EventMetadata } from "../../src/events/types.ts";
import type { CoreMemoryRepository } from "../../src/memory/core.ts";
import { EMBEDDING_DIM, type EmbeddingService } from "../../src/memory/embeddings.ts";
import type { EpisodicRepository } from "../../src/memory/episodic.ts";
import type { RetrievalService } from "../../src/memory/retrieval.ts";
import type { SelfModelRepository } from "../../src/memory/self_model.ts";
import type { SkillRepository } from "../../src/memory/skills.ts";
import type { CoreMemorySlot, JsonValue } from "../../src/memory/types.ts";
import type { UserModelRepository } from "../../src/memory/user_model.ts";

// ---------------------------------------------------------------------------
// Stub EventBus
// ---------------------------------------------------------------------------

interface EmittedEvent {
	readonly type: Event["type"];
	readonly actor: Event["actor"];
	readonly data: Record<string, unknown>;
	readonly metadata: EventMetadata;
}

interface EmittedEphemeral {
	readonly type: string;
	readonly data: unknown;
}

interface StubBus {
	readonly bus: EventBus;
	readonly events: EmittedEvent[];
	readonly ephemeral: EmittedEphemeral[];
}

function createStubBus(): StubBus {
	const events: EmittedEvent[] = [];
	const ephemeral: EmittedEphemeral[] = [];
	const bus = {
		on(): void {},
		onEphemeral(): () => void {
			return () => {};
		},
		async emit(event: Omit<Event, "id" | "timestamp">): Promise<Event> {
			events.push({
				type: event.type,
				actor: event.actor,
				data: event.data as unknown as Record<string, unknown>,
				metadata: event.metadata,
			});
			return event as unknown as Event;
		},
		emitEphemeral(event: { type: string; data: unknown }): void {
			ephemeral.push({ type: event.type, data: event.data });
		},
		async start(): Promise<void> {},
		async stop(): Promise<void> {},
		async flush(): Promise<void> {},
	};
	return { bus: bus as unknown as EventBus, events, ephemeral };
}

// ---------------------------------------------------------------------------
// Stub repositories — minimal surface used by the engine + prompt assembly
// ---------------------------------------------------------------------------

const POPULATED_PERSONA: JsonValue = {
	name: "Theo",
	voice: { tone: "warm", style: "first-person singular" },
};
const POPULATED_GOALS: JsonValue = {
	primary: { description: "help the owner today", status: "ongoing" },
};

interface CoreState {
	persona: JsonValue;
	goals: JsonValue;
	context: JsonValue;
	hash: string;
}

function stubCore(state: CoreState): CoreMemoryRepository {
	const repo = {
		async readSlot(slot: CoreMemorySlot): Promise<Result<JsonValue, Error>> {
			if (slot === "persona") return ok(state.persona);
			if (slot === "goals") return ok(state.goals);
			if (slot === "context") return ok(state.context);
			return ok({});
		},
		async read() {
			throw new Error("stubCore.read should not be called");
		},
		async update() {
			throw new Error("stubCore.update should not be called");
		},
		async hash() {
			return state.hash;
		},
	};
	return repo as unknown as CoreMemoryRepository;
}

function stubFailingCore(): CoreMemoryRepository {
	// Simulates a missing core memory slot (e.g. manual DELETE, bad migration).
	// assembleSystemPrompt() propagates the error via the Result unwrap branch,
	// which the engine converts to turn.failed.
	const repo = {
		async readSlot() {
			return {
				ok: false as const,
				error: new Error("Core memory slot not found: persona"),
			};
		},
		async read() {
			throw new Error("unused");
		},
		async update() {
			throw new Error("unused");
		},
		async hash() {
			return "failing";
		},
	};
	return repo as unknown as CoreMemoryRepository;
}

function stubUserModel(): UserModelRepository {
	return {
		async getDimensions() {
			return [];
		},
		async getDimension() {
			return null;
		},
		async updateDimension() {
			throw new Error("unused");
		},
	};
}

function stubRetrieval(): RetrievalService {
	return {
		async search() {
			return [];
		},
	} as unknown as RetrievalService;
}

function stubSkills(): SkillRepository {
	return {
		async create() {
			throw new Error("stubSkills.create should not be called");
		},
		async findByTrigger() {
			return [];
		},
		async recordOutcome() {
			throw new Error("stubSkills.recordOutcome should not be called");
		},
		async promote() {
			throw new Error("stubSkills.promote should not be called");
		},
		async getById() {
			return null;
		},
	};
}

function stubSelfModel(): SelfModelRepository {
	return {
		async recordPrediction(): Promise<void> {},
		async recordOutcome(): Promise<void> {},
		async getCalibration() {
			return 0;
		},
		async getLifetimeCalibration() {
			return 0;
		},
		async getDomain() {
			return null;
		},
	};
}

function stubEmbeddings(): EmbeddingService {
	return {
		async embed(): Promise<Float32Array> {
			return new Float32Array(EMBEDDING_DIM);
		},
		async embedBatch(): Promise<readonly Float32Array[]> {
			return [];
		},
		async warmup(): Promise<void> {},
	};
}

function stubEpisodic(): EpisodicRepository {
	const repo = {
		async append() {
			return {
				id: 1 as never,
				sessionId: "",
				role: "user" as const,
				body: "",
				embedding: null,
				supersededBy: null,
				createdAt: new Date(0),
			};
		},
		async getBySession() {
			return [];
		},
		async linkToNode() {},
	};
	return repo as unknown as EpisodicRepository;
}

// ---------------------------------------------------------------------------
// MCP server stub — Options demands a concrete instance; we only need
// something type-compatible because the SDK is mocked out.
// ---------------------------------------------------------------------------

const FAKE_MEMORY_SERVER = {
	type: "sdk",
	name: "memory",
	instance: {} as never,
} as unknown as McpSdkServerConfigWithInstance;

// ---------------------------------------------------------------------------
// SDK message factories
// ---------------------------------------------------------------------------

function successResult(options: {
	readonly result: string;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly costUsd: number;
}): SDKMessage {
	return {
		type: "result",
		subtype: "success",
		duration_ms: 10,
		duration_api_ms: 5,
		is_error: false,
		num_turns: 1,
		result: options.result,
		stop_reason: "end_turn",
		total_cost_usd: options.costUsd,
		usage: {
			input_tokens: options.inputTokens,
			output_tokens: options.outputTokens,
			cache_creation_input_tokens: 0,
			cache_read_input_tokens: 0,
		},
		modelUsage: {},
		permission_denials: [],
	} as unknown as SDKMessage;
}

function errorResult(
	subtype:
		| "error_during_execution"
		| "error_max_turns"
		| "error_max_budget_usd"
		| "error_max_structured_output_retries",
	errors: readonly string[],
): SDKMessage {
	return {
		type: "result",
		subtype,
		duration_ms: 10,
		duration_api_ms: 5,
		is_error: true,
		num_turns: 1,
		stop_reason: null,
		total_cost_usd: 0,
		usage: {
			input_tokens: 0,
			output_tokens: 0,
			cache_creation_input_tokens: 0,
			cache_read_input_tokens: 0,
		},
		modelUsage: {},
		permission_denials: [],
		errors: [...errors],
	} as unknown as SDKMessage;
}

function assistantText(text: string): SDKMessage {
	return {
		type: "assistant",
		parent_tool_use_id: null,
		session_id: "",
		uuid: "00000000-0000-0000-0000-000000000000",
		message: {
			type: "message",
			id: "msg_1",
			role: "assistant",
			model: "claude-sonnet-4-6",
			content: [{ type: "text", text, citations: null }],
			stop_reason: "end_turn",
			stop_sequence: null,
			usage: {
				input_tokens: 0,
				output_tokens: 0,
				cache_creation_input_tokens: 0,
				cache_read_input_tokens: 0,
			},
		},
	} as unknown as SDKMessage;
}

function assistantToolUse(options: {
	readonly callId: string;
	readonly name: string;
	readonly input: Record<string, unknown>;
}): SDKMessage {
	return {
		type: "assistant",
		parent_tool_use_id: null,
		session_id: "",
		uuid: "00000000-0000-0000-0000-000000000000",
		message: {
			type: "message",
			id: "msg_2",
			role: "assistant",
			model: "claude-sonnet-4-6",
			content: [
				{
					type: "tool_use",
					id: options.callId,
					name: options.name,
					input: options.input,
				},
			],
			stop_reason: "tool_use",
			stop_sequence: null,
			usage: {
				input_tokens: 0,
				output_tokens: 0,
				cache_creation_input_tokens: 0,
				cache_read_input_tokens: 0,
			},
		},
	} as unknown as SDKMessage;
}

function userToolResult(callId: string, content = "ok"): SDKMessage {
	return {
		type: "user",
		parent_tool_use_id: null,
		session_id: "",
		uuid: "00000000-0000-0000-0000-000000000000",
		message: {
			role: "user",
			content: [{ type: "tool_result", tool_use_id: callId, content }],
		},
	} as unknown as SDKMessage;
}

function streamTextDelta(text: string): SDKMessage {
	return {
		type: "stream_event",
		parent_tool_use_id: null,
		session_id: "",
		uuid: "00000000-0000-0000-0000-000000000000",
		event: {
			type: "content_block_delta",
			index: 0,
			delta: { type: "text_delta", text },
		},
	} as unknown as SDKMessage;
}

/** Build a queryFn that yields the provided messages in order. */
function mockQueryFn(
	messages: readonly SDKMessage[],
	captured?: { lastOptions?: Options | undefined; lastPrompt?: string },
): (params: { prompt: string; options?: Options }) => Query {
	return ({ prompt, options }) => {
		if (captured) {
			captured.lastOptions = options;
			captured.lastPrompt = prompt;
		}
		async function* gen(): AsyncGenerator<SDKMessage, void> {
			for (const m of messages) yield m;
		}
		return gen() as unknown as Query;
	};
}

/** queryFn whose generator throws on first next() — simulates a mid-stream crash. */
function throwingQueryFn(message: string): (p: { prompt: string; options?: Options }) => Query {
	return () => {
		const iter: AsyncIterator<SDKMessage, void, undefined> = {
			next: () => Promise.reject(new Error(message)),
			return: () => Promise.resolve({ done: true as const, value: undefined }),
			throw: () => Promise.reject(new Error(message)),
		};
		const gen = {
			[Symbol.asyncIterator]: () => iter,
			next: iter.next.bind(iter),
			return: iter.return?.bind(iter),
			throw: iter.throw?.bind(iter),
		};
		return gen as unknown as Query;
	};
}

// ---------------------------------------------------------------------------
// Engine builder
// ---------------------------------------------------------------------------

function buildEngine(overrides?: {
	readonly core?: CoreMemoryRepository;
	readonly queryFn?: (params: { prompt: string; options?: Options }) => Query;
	readonly maxBudgetPerTurn?: number;
	readonly agents?: Record<string, import("@anthropic-ai/claude-agent-sdk").AgentDefinition>;
	readonly advisorModel?: string;
}): {
	readonly engine: ChatEngine;
	readonly bus: StubBus;
	readonly sessions: SessionManager;
} {
	const bus = createStubBus();
	const embeddings = stubEmbeddings();
	const selfModel = stubSelfModel();
	const sessions = new SessionManager(embeddings, selfModel, {
		inactivityTimeoutMs: 60_000,
	});
	const core =
		overrides?.core ??
		stubCore({
			persona: POPULATED_PERSONA,
			goals: POPULATED_GOALS,
			context: {},
			hash: "stable",
		});

	const context: ContextDependencies = {
		coreMemory: core,
		userModel: stubUserModel(),
		retrieval: stubRetrieval(),
		skills: stubSkills(),
		embeddings,
	};

	const engine = new ChatEngine({
		bus: bus.bus,
		sessions,
		memoryServer: FAKE_MEMORY_SERVER,
		coreMemory: core,
		episodic: stubEpisodic(),
		context,
		...(overrides?.queryFn ? { queryFn: overrides.queryFn } : {}),
		...(overrides?.maxBudgetPerTurn !== undefined
			? { config: { maxBudgetPerTurn: overrides.maxBudgetPerTurn } }
			: {}),
		...(overrides?.agents !== undefined ? { agents: overrides.agents } : {}),
		...(overrides?.advisorModel !== undefined ? { advisorModel: overrides.advisorModel } : {}),
	});
	return { engine, bus, sessions };
}

// ---------------------------------------------------------------------------
// Happy path: successful turn
// ---------------------------------------------------------------------------

describe("ChatEngine.handleMessage — success path", () => {
	test("emits message.received, session.created, turn.started, turn.completed in order", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				successResult({ result: "hi there", inputTokens: 12, outputTokens: 4, costUsd: 0.01 }),
			]),
		});

		const result = await engine.handleMessage("hello", "cli");

		expect(result).toEqual({ ok: true, response: "hi there" });
		const types = bus.events.map((e) => e.type);
		expect(types).toEqual([
			"message.received",
			"session.created",
			"turn.started",
			"turn.completed",
			"cloud_egress.turn",
		]);
	});

	test("turn.completed carries token counts and cost from the SDK result", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				successResult({ result: "ok", inputTokens: 100, outputTokens: 50, costUsd: 0.25 }),
			]),
		});

		await engine.handleMessage("do work", "cli");

		const completed = bus.events.find((e) => e.type === "turn.completed");
		expect(completed).toBeDefined();
		expect(completed?.data).toMatchObject({
			inputTokens: 100,
			outputTokens: 50,
			totalTokens: 150,
			costUsd: 0.25,
			responseBody: "ok",
		});
	});

	test("message.received fires first with body + channel", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi Theo", "telegram");

		expect(bus.events[0]).toMatchObject({
			type: "message.received",
			data: { body: "hi Theo", channel: "telegram" },
		});
	});
});

// ---------------------------------------------------------------------------
// Streaming: stream.chunk events from text_delta
// ---------------------------------------------------------------------------

describe("ChatEngine.handleMessage — streaming", () => {
	test("emits stream.chunk ephemeral events for each text_delta", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				streamTextDelta("Hel"),
				streamTextDelta("lo"),
				assistantText("Hello"),
				successResult({ result: "Hello", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		const chunks = bus.ephemeral.filter((e) => e.type === "stream.chunk");
		expect(chunks).toHaveLength(2);
		expect(chunks[0]?.data).toMatchObject({ text: "Hel" });
		expect(chunks[1]?.data).toMatchObject({ text: "lo" });
	});

	test("non-text_delta stream events are ignored", async () => {
		const nonTextStream = {
			type: "stream_event",
			parent_tool_use_id: null,
			session_id: "",
			uuid: "00000000-0000-0000-0000-000000000000",
			event: {
				type: "content_block_delta",
				index: 0,
				delta: { type: "thinking_delta", thinking: "internal" },
			},
		} as unknown as SDKMessage;

		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				nonTextStream,
				successResult({ result: "x", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		expect(bus.ephemeral.filter((e) => e.type === "stream.chunk")).toHaveLength(0);
	});

	test("emits stream.done after a successful turn", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		const done = bus.ephemeral.filter((e) => e.type === "stream.done");
		expect(done).toHaveLength(1);
	});

	test("emits stream.done even when the generator throws mid-stream", async () => {
		const { engine, bus } = buildEngine({
			queryFn: throwingQueryFn("boom"),
		});

		await engine.handleMessage("hi", "cli");

		const done = bus.ephemeral.filter((e) => e.type === "stream.done");
		expect(done).toHaveLength(1);
	});
});

// ---------------------------------------------------------------------------
// Tool lifecycle ephemerals: tool.start / tool.done
// ---------------------------------------------------------------------------

describe("ChatEngine.handleMessage — tool lifecycle", () => {
	test("emits tool.start for each tool_use block in an assistant message", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				assistantToolUse({
					callId: "call_1",
					name: "mcp__memory__store_memory",
					input: { body: "note" },
				}),
				userToolResult("call_1"),
				successResult({ result: "done", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		const starts = bus.ephemeral.filter((e) => e.type === "tool.start");
		expect(starts).toHaveLength(1);
		expect(starts[0]?.data).toMatchObject({
			callId: "call_1",
			name: "mcp__memory__store_memory",
		});
	});

	test("emits tool.done with a non-negative duration after tool_result", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				assistantToolUse({
					callId: "call_2",
					name: "mcp__memory__retrieve",
					input: { query: "x" },
				}),
				userToolResult("call_2"),
				successResult({ result: "done", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		const dones = bus.ephemeral.filter((e) => e.type === "tool.done");
		expect(dones).toHaveLength(1);
		const data = dones[0]?.data as { callId: string; durationMs: number };
		expect(data.callId).toBe("call_2");
		expect(data.durationMs).toBeGreaterThanOrEqual(0);
	});

	test("orphan tool_result (no matching tool.start) is silently dropped", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				userToolResult("unknown_call"),
				successResult({ result: "done", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("hi", "cli");

		expect(bus.ephemeral.filter((e) => e.type === "tool.done")).toHaveLength(0);
	});
});

// ---------------------------------------------------------------------------
// Abort: gate-triggered cancellation
// ---------------------------------------------------------------------------

describe("ChatEngine.abortCurrentTurn", () => {
	test("is a no-op when idle (no throw)", () => {
		const { engine } = buildEngine({
			queryFn: mockQueryFn([]),
		});
		expect(() => {
			engine.abortCurrentTurn();
		}).not.toThrow();
	});

	test("passes a live AbortController to the SDK during a turn", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		const ac = captured.lastOptions?.abortController;
		expect(ac).toBeInstanceOf(AbortController);
		// Not aborted — the turn completed cleanly.
		expect(ac?.signal.aborted).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// Failure modes
// ---------------------------------------------------------------------------

describe("ChatEngine.handleMessage — failure paths", () => {
	test("SDK returns error_during_execution: turn.failed event, ok=false", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([errorResult("error_during_execution", ["model melted"])]),
		});

		const result = await engine.handleMessage("hi", "cli");

		expect(result.ok).toBe(false);
		if (result.ok) throw new Error("unreachable");
		expect(result.error).toBe("error_during_execution");

		const failed = bus.events.find((e) => e.type === "turn.failed");
		expect(failed).toBeDefined();
		expect(failed?.data).toMatchObject({
			errorType: "error_during_execution",
			errors: ["model melted"],
		});
	});

	test("SDK returns error_max_budget_usd: errorType propagates to turn.failed", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([errorResult("error_max_budget_usd", ["budget exceeded"])]),
		});

		await engine.handleMessage("hi", "cli");

		const failed = bus.events.find((e) => e.type === "turn.failed");
		expect(failed?.data).toMatchObject({ errorType: "error_max_budget_usd" });
	});

	test("generator throws mid-stream: turn.failed event, ok=false, no crash", async () => {
		const { engine, bus } = buildEngine({
			queryFn: throwingQueryFn("network partition"),
		});

		const result = await engine.handleMessage("hi", "cli");

		expect(result.ok).toBe(false);
		if (result.ok) throw new Error("unreachable");
		expect(result.error).toBe("network partition");
		expect(bus.events.find((e) => e.type === "turn.failed")).toBeDefined();
	});

	test("assembly error (missing core slot): turn.failed emitted, ok=false, no turn.started", async () => {
		const { engine, bus } = buildEngine({
			core: stubFailingCore(),
			queryFn: mockQueryFn([]), // never called
		});

		const result = await engine.handleMessage("hi", "cli");

		expect(result.ok).toBe(false);
		if (result.ok) throw new Error("unreachable");
		expect(result.error).toMatch(/Core memory slot not found/);

		// Exactly one turn.failed; no turn.started because we never got to SDK.
		const types = bus.events.map((e) => e.type);
		expect(types).toContain("message.received");
		expect(types).toContain("turn.failed");
		expect(types).not.toContain("turn.started");
		expect(types).not.toContain("turn.completed");
	});
});

// ---------------------------------------------------------------------------
// Session management integration
// ---------------------------------------------------------------------------

describe("ChatEngine session management", () => {
	test("two messages within timeout: session is reused (no second session.created)", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("first", "cli");
		// Reset captured SDK messages with a fresh queue for the second turn.
		await engine.handleMessage("second", "cli");

		const createdCount = bus.events.filter((e) => e.type === "session.created").length;
		expect(createdCount).toBe(1);
	});

	test("core memory hash change: session rotates, session.released fires", async () => {
		const hashState = { h: "H1" };
		const core = {
			async readSlot(slot: CoreMemorySlot): Promise<Result<JsonValue, Error>> {
				if (slot === "persona") return ok(POPULATED_PERSONA);
				if (slot === "goals") return ok(POPULATED_GOALS);
				return ok({});
			},
			async read() {
				throw new Error("unused");
			},
			async update() {
				throw new Error("unused");
			},
			async hash() {
				return hashState.h;
			},
		} as unknown as CoreMemoryRepository;

		const { engine, bus } = buildEngine({
			core,
			queryFn: mockQueryFn([
				successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("first", "cli");
		hashState.h = "H2"; // core memory mutated between turns
		await engine.handleMessage("second", "cli");

		const released = bus.events.filter((e) => e.type === "session.released");
		expect(released).toHaveLength(1);
		expect(released[0]?.data).toMatchObject({ reason: "core_memory_changed" });

		const created = bus.events.filter((e) => e.type === "session.created");
		expect(created).toHaveLength(2);
	});

	test("resetSession: emits session.released, clears active session", async () => {
		const { engine, bus } = buildEngine({
			queryFn: mockQueryFn([
				successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		await engine.handleMessage("first", "cli");
		await engine.resetSession("user_wants_fresh");

		const released = bus.events.filter((e) => e.type === "session.released");
		expect(released).toHaveLength(1);
		expect(released[0]?.data).toMatchObject({ reason: "user_wants_fresh" });
	});
});

// ---------------------------------------------------------------------------
// Options propagated to the SDK
// ---------------------------------------------------------------------------

describe("ChatEngine SDK options", () => {
	test("maxBudgetUsd defaults to 0.5 and is set on every query()", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(captured.lastOptions?.maxBudgetUsd).toBe(0.5);
	});

	test("maxBudgetPerTurn config override propagates as maxBudgetUsd", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			maxBudgetPerTurn: 2.5,
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(captured.lastOptions?.maxBudgetUsd).toBe(2.5);
	});

	test("settingSources is an empty array (external-config isolation)", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(captured.lastOptions?.settingSources).toEqual([]);
	});

	test("allowedTools auto-approves mcp__memory__*", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(captured.lastOptions?.allowedTools).toEqual(["mcp__memory__*"]);
	});

	test("system prompt is non-empty and passed to the SDK", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(typeof captured.lastOptions?.systemPrompt).toBe("string");
		expect((captured.lastOptions?.systemPrompt as string).length).toBeGreaterThanOrEqual(50);
	});

	test("prompt forwarded to the SDK matches the user message", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("plan my day", "cli");

		expect(captured.lastPrompt).toBe("plan my day");
	});
});

// ---------------------------------------------------------------------------
// Token extraction fallback: assistant message text
// ---------------------------------------------------------------------------

describe("ChatEngine response extraction", () => {
	test("result.result overrides any prior assistant-message text", async () => {
		const { engine } = buildEngine({
			queryFn: mockQueryFn([
				assistantText("draft text"),
				successResult({ result: "final text", inputTokens: 1, outputTokens: 1, costUsd: 0 }),
			]),
		});

		const result = await engine.handleMessage("hi", "cli");

		expect(result).toEqual({ ok: true, response: "final text" });
	});
});

// ---------------------------------------------------------------------------
// Subagent delegation + advisor settings
// ---------------------------------------------------------------------------

describe("ChatEngine subagent delegation", () => {
	test("agents option is forwarded to the SDK as options.agents", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const agents = {
			writer: { description: "writer", prompt: "write", model: "sonnet", maxTurns: 10 },
		};
		const { engine } = buildEngine({
			agents,
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("draft something", "cli");

		expect(captured.lastOptions?.agents).toBeDefined();
		expect(captured.lastOptions?.agents?.["writer"]).toBeDefined();
	});

	test("advisorModel is forwarded via options.settings.advisorModel", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			advisorModel: "claude-opus-4-6",
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		const settings = captured.lastOptions?.settings;
		expect(settings).toBeDefined();
		if (settings !== undefined && typeof settings === "object") {
			expect((settings as { advisorModel?: string }).advisorModel).toBe("claude-opus-4-6");
		}
	});

	test("omitting agents and advisorModel leaves both fields undefined", async () => {
		const captured: { lastOptions?: Options | undefined; lastPrompt?: string } = {};
		const { engine } = buildEngine({
			queryFn: mockQueryFn(
				[successResult({ result: "ok", inputTokens: 1, outputTokens: 1, costUsd: 0 })],
				captured,
			),
		});

		await engine.handleMessage("hi", "cli");

		expect(captured.lastOptions?.agents).toBeUndefined();
		expect(captured.lastOptions?.settings).toBeUndefined();
	});
});
