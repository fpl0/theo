/**
 * Unit tests for the SDK hook layer.
 *
 * Covers:
 *   - `parseTranscript()` — JSON-lines transcript parsing, robust to junk
 *   - `safeHook()` — exceptions are converted to `hook.failed` events, the
 *     turn continues with `{}` as the hook result
 *   - `buildHooks()` — registers hooks on the expected SDK events and the
 *     PreToolUse matcher is scoped to `mcp__memory__store_memory`
 *   - Individual hook behaviors: UserPromptSubmit / PreToolUse allow+deny /
 *     PreCompact (with + without transcript) / PostCompact / Stop
 *
 * Each test uses a stub EventBus that records emissions, and a stub
 * EpisodicRepository.append that records calls — no database or SDK
 * subprocess involved.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { buildHooks, parseTranscript, safeHook } from "../../src/chat/hooks.ts";
import type { EventBus } from "../../src/events/bus.ts";
import type { Event, EventMetadata } from "../../src/events/types.ts";
import type { EpisodicRepository } from "../../src/memory/episodic.ts";
import type { CreateEpisodeInput, Episode } from "../../src/memory/types.ts";
import { asEpisodeId } from "../../src/memory/types.ts";

// ---------------------------------------------------------------------------
// Stubs
// ---------------------------------------------------------------------------

interface EmittedEvent {
	readonly type: Event["type"];
	readonly actor: Event["actor"];
	readonly data: unknown;
	readonly metadata: EventMetadata;
}

interface StubBus {
	readonly bus: EventBus;
	readonly events: EmittedEvent[];
}

function createStubBus(): StubBus {
	const events: EmittedEvent[] = [];
	const bus = {
		on(): void {},
		onEphemeral(): void {},
		async emit(event: Omit<Event, "id" | "timestamp">) {
			events.push({
				type: event.type,
				actor: event.actor,
				data: event.data,
				metadata: event.metadata,
			});
			// The returned value is never used by hook code paths.
			return event as unknown as Event;
		},
		emitEphemeral(): void {},
		async start(): Promise<void> {},
		async stop(): Promise<void> {},
		async flush(): Promise<void> {},
	};
	return { bus: bus as unknown as EventBus, events };
}

interface StubEpisodic {
	readonly repo: EpisodicRepository;
	readonly calls: CreateEpisodeInput[];
}

function createStubEpisodic(): StubEpisodic {
	const calls: CreateEpisodeInput[] = [];
	let nextId = 1;
	const repo = {
		async append(input: CreateEpisodeInput): Promise<Episode> {
			calls.push(input);
			const id = nextId++;
			return {
				id: asEpisodeId(id),
				sessionId: input.sessionId,
				role: input.role,
				body: input.body,
				embedding: null,
				supersededBy: null,
				createdAt: new Date(id),
			};
		},
		async getBySession(): Promise<readonly Episode[]> {
			return [];
		},
		async linkToNode(): Promise<void> {},
	};
	return { repo: repo as unknown as EpisodicRepository, calls };
}

/** Build a minimal abort signal for hook callbacks. */
function fakeAbort(): AbortSignal {
	const ctrl = new AbortController();
	return ctrl.signal;
}

// ---------------------------------------------------------------------------
// parseTranscript
// ---------------------------------------------------------------------------

describe("parseTranscript", () => {
	test("empty string produces no messages", () => {
		expect(parseTranscript("")).toEqual([]);
	});

	test("whitespace-only string produces no messages", () => {
		expect(parseTranscript("   \n\n\t\n")).toEqual([]);
	});

	test("parses well-formed JSONL with user + assistant roles", () => {
		const raw = '{"role":"user","content":"hi"}\n' + '{"role":"assistant","content":"hello"}\n';

		expect(parseTranscript(raw)).toEqual([
			{ role: "user", text: "hi" },
			{ role: "assistant", text: "hello" },
		]);
	});

	test("skips unparseable JSON lines without crashing", () => {
		const raw =
			'{"role":"user","content":"hi"}\n' +
			"this is not JSON\n" +
			'{"role":"assistant","content":"hello"}\n';

		expect(parseTranscript(raw)).toEqual([
			{ role: "user", text: "hi" },
			{ role: "assistant", text: "hello" },
		]);
	});

	test("skips records without role or without content", () => {
		const raw =
			'{"role":"assistant"}\n' +
			'{"content":"orphan"}\n' +
			'{"role":"system","content":"meta"}\n' + // valid JSON but wrong role
			'{"role":"assistant","content":"kept"}\n';

		expect(parseTranscript(raw)).toEqual([{ role: "assistant", text: "kept" }]);
	});
});

// ---------------------------------------------------------------------------
// safeHook
// ---------------------------------------------------------------------------

describe("safeHook", () => {
	test("forwards return value when the underlying hook succeeds", async () => {
		const { bus, events } = createStubBus();
		const wrapped = safeHook(async () => ({ async: true as const }), bus);

		const result = await wrapped({ hook_event_name: "Stop" } as never, undefined, {
			signal: fakeAbort(),
		});

		expect(result).toEqual({ async: true });
		expect(events).toHaveLength(0);
	});

	test("thrown exception → hook.failed event + empty result, turn continues", async () => {
		const { bus, events } = createStubBus();
		const wrapped = safeHook(async () => {
			throw new Error("boom");
		}, bus);

		const result = await wrapped({ hook_event_name: "Stop" } as never, undefined, {
			signal: fakeAbort(),
		});

		expect(result).toEqual({});
		expect(events).toHaveLength(1);
		const event = events[0];
		expect(event?.type).toBe("hook.failed");
		expect(event?.actor).toBe("system");
		expect(event?.data).toMatchObject({ hookEvent: "Stop", error: "boom" });
	});

	test("non-Error thrown values are stringified", async () => {
		const { bus, events } = createStubBus();
		const wrapped = safeHook(async () => {
			throw "string failure";
		}, bus);

		await wrapped({ hook_event_name: "UserPromptSubmit" } as never, undefined, {
			signal: fakeAbort(),
		});

		expect(events[0]?.data).toMatchObject({ error: "string failure" });
	});
});

// ---------------------------------------------------------------------------
// buildHooks: topology
// ---------------------------------------------------------------------------

describe("buildHooks topology", () => {
	test("registers UserPromptSubmit / PreToolUse / PreCompact / PostCompact / Stop", () => {
		const { bus } = createStubBus();
		const { repo } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier: "owner",
		});

		expect(Object.keys(hooks).sort()).toEqual([
			"PostCompact",
			"PreCompact",
			"PreToolUse",
			"Stop",
			"UserPromptSubmit",
		]);
	});

	test("PreToolUse matcher is scoped to mcp__memory__store_memory", () => {
		const { bus } = createStubBus();
		const { repo } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier: "owner",
		});

		const matchers = hooks["PreToolUse"];
		expect(matchers).toBeDefined();
		expect(matchers).toHaveLength(1);
		expect(matchers?.[0]?.matcher).toBe("mcp__memory__store_memory");
	});
});

// ---------------------------------------------------------------------------
// UserPromptSubmit
// ---------------------------------------------------------------------------

describe("UserPromptSubmit hook", () => {
	test("persists the prompt as a user-role episode", async () => {
		const { bus } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-42",
			trustTier: "owner",
		});
		const cb = hooks["UserPromptSubmit"]?.[0]?.hooks[0];
		expect(cb).toBeDefined();
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "UserPromptSubmit",
				session_id: "sess-42",
				transcript_path: "",
				cwd: "/",
				prompt: "hello Theo",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(calls).toEqual([
			{
				sessionId: "sess-42",
				role: "user",
				body: "hello Theo",
				actor: "user",
			},
		]);
	});
});

// ---------------------------------------------------------------------------
// PreToolUse (privacy gate)
// ---------------------------------------------------------------------------

describe("PreToolUse hook", () => {
	function preToolUseCb(trustTier: Parameters<typeof buildHooks>[0]["trustTier"]) {
		const { bus } = createStubBus();
		const { repo } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier,
		});
		const cb = hooks["PreToolUse"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");
		return cb;
	}

	test("allows clean body for owner tier", async () => {
		const cb = preToolUseCb("owner");

		const result = await cb(
			{
				hook_event_name: "PreToolUse",
				session_id: "sess-1",
				transcript_path: "",
				cwd: "/",
				tool_name: "mcp__memory__store_memory",
				tool_input: { body: "User prefers dark mode" },
				tool_use_id: "t1",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
	});

	test("denies sensitive content when trust tier disallows it", async () => {
		// inferred tier max-allowed = "none" — an email should trip "sensitive".
		const cb = preToolUseCb("inferred");

		const result = await cb(
			{
				hook_event_name: "PreToolUse",
				session_id: "sess-1",
				transcript_path: "",
				cwd: "/",
				tool_name: "mcp__memory__store_memory",
				tool_input: { body: "ping alice@example.com about the demo" },
				tool_use_id: "t1",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({
			hookSpecificOutput: {
				hookEventName: "PreToolUse",
				permissionDecision: "deny",
				permissionDecisionReason: expect.stringContaining("email address"),
			},
		});
	});

	test("returns empty result when tool_input.body is missing/non-string", async () => {
		const cb = preToolUseCb("owner");

		const result = await cb(
			{
				hook_event_name: "PreToolUse",
				session_id: "sess-1",
				transcript_path: "",
				cwd: "/",
				tool_name: "mcp__memory__store_memory",
				tool_input: { payload: 42 },
				tool_use_id: "t1",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
	});
});

// ---------------------------------------------------------------------------
// PreCompact
// ---------------------------------------------------------------------------

describe("PreCompact hook", () => {
	let tmpPath = "";

	beforeEach(() => {
		tmpPath = join(process.cwd(), `.tmp_precompact_${Date.now().toString()}.jsonl`);
	});

	afterEach(async () => {
		await rm(tmpPath, { force: true });
	});

	test("archives assistant messages from the transcript as episodes", async () => {
		const transcript =
			'{"role":"user","content":"hi"}\n' +
			'{"role":"assistant","content":"hello there"}\n' +
			'{"role":"user","content":"what\'s up"}\n' +
			'{"role":"assistant","content":"working on Theo"}\n';
		await writeFile(tmpPath, transcript, "utf8");

		const { bus, events } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-99",
			trustTier: "owner",
		});
		const cb = hooks["PreCompact"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "PreCompact",
				session_id: "sess-99",
				transcript_path: tmpPath,
				cwd: "/",
				trigger: "manual",
				custom_instructions: null,
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		// User-role transcript entries are NOT archived; they were captured by
		// UserPromptSubmit at send time.
		expect(calls).toEqual([
			{
				sessionId: "sess-99",
				role: "assistant",
				body: "hello there",
				actor: "theo",
			},
			{
				sessionId: "sess-99",
				role: "assistant",
				body: "working on Theo",
				actor: "theo",
			},
		]);
		// session.compacting event fires with the trigger forwarded.
		expect(events).toEqual([
			{
				type: "session.compacting",
				actor: "system",
				data: { sessionId: "sess-99", trigger: "manual" },
				metadata: { sessionId: "sess-99" },
			},
		]);
	});

	test("missing transcript file: no episodes, no crash", async () => {
		const { bus, events } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier: "owner",
		});
		const cb = hooks["PreCompact"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "PreCompact",
				session_id: "sess-1",
				transcript_path: "/this/path/does/not/exist.jsonl",
				cwd: "/",
				trigger: "auto",
				custom_instructions: null,
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(calls).toEqual([]);
		// session.compacting is still emitted before the file read attempt.
		expect(events).toHaveLength(1);
		expect(events[0]?.type).toBe("session.compacting");
	});

	test("empty transcript_path string: no episodes, no crash", async () => {
		const { bus } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier: "owner",
		});
		const cb = hooks["PreCompact"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "PreCompact",
				session_id: "sess-1",
				transcript_path: "",
				cwd: "/",
				trigger: "auto",
				custom_instructions: null,
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(calls).toEqual([]);
	});
});

// ---------------------------------------------------------------------------
// PostCompact
// ---------------------------------------------------------------------------

describe("PostCompact hook", () => {
	test("emits session.compacted with the SDK-provided summary", async () => {
		const { bus, events } = createStubBus();
		const { repo } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-7",
			trustTier: "owner",
		});
		const cb = hooks["PostCompact"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "PostCompact",
				session_id: "sess-7",
				transcript_path: "",
				cwd: "/",
				trigger: "auto",
				compact_summary: "short summary",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(events).toEqual([
			{
				type: "session.compacted",
				actor: "system",
				data: { sessionId: "sess-7", summary: "short summary" },
				metadata: { sessionId: "sess-7" },
			},
		]);
	});
});

// ---------------------------------------------------------------------------
// Stop
// ---------------------------------------------------------------------------

describe("Stop hook", () => {
	test("archives the assistant's last message as an episode", async () => {
		const { bus } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-5",
			trustTier: "owner",
		});
		const cb = hooks["Stop"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "Stop",
				session_id: "sess-5",
				transcript_path: "",
				cwd: "/",
				stop_hook_active: false,
				last_assistant_message: "final answer",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(calls).toEqual([
			{
				sessionId: "sess-5",
				role: "assistant",
				body: "final answer",
				actor: "theo",
			},
		]);
	});

	test("no last_assistant_message: no episode appended, no crash", async () => {
		const { bus } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-5",
			trustTier: "owner",
		});
		const cb = hooks["Stop"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "Stop",
				session_id: "sess-5",
				transcript_path: "",
				cwd: "/",
				stop_hook_active: false,
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(calls).toEqual([]);
	});

	test("empty last_assistant_message: no episode appended", async () => {
		const { bus } = createStubBus();
		const { repo, calls } = createStubEpisodic();
		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-5",
			trustTier: "owner",
		});
		const cb = hooks["Stop"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		await cb(
			{
				hook_event_name: "Stop",
				session_id: "sess-5",
				transcript_path: "",
				cwd: "/",
				stop_hook_active: false,
				last_assistant_message: "",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(calls).toEqual([]);
	});
});

// ---------------------------------------------------------------------------
// Integration: safeHook wraps every builder output
// ---------------------------------------------------------------------------

describe("buildHooks safety net", () => {
	test("a throwing repository is caught and surfaced as hook.failed", async () => {
		const { bus, events } = createStubBus();
		const repo = {
			async append(): Promise<Episode> {
				throw new Error("db down");
			},
			async getBySession(): Promise<readonly Episode[]> {
				return [];
			},
			async linkToNode(): Promise<void> {},
		} as unknown as EpisodicRepository;

		const hooks = buildHooks({
			bus,
			episodic: repo,
			sessionId: "sess-1",
			trustTier: "owner",
		});
		const cb = hooks["UserPromptSubmit"]?.[0]?.hooks[0];
		if (!cb) throw new Error("unreachable");

		const result = await cb(
			{
				hook_event_name: "UserPromptSubmit",
				session_id: "sess-1",
				transcript_path: "",
				cwd: "/",
				prompt: "hi",
			} as never,
			undefined,
			{ signal: fakeAbort() },
		);

		expect(result).toEqual({});
		expect(events).toHaveLength(1);
		expect(events[0]?.type).toBe("hook.failed");
		expect(events[0]?.data).toMatchObject({
			hookEvent: "UserPromptSubmit",
			error: "db down",
		});
	});
});
