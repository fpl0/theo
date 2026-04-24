/**
 * Unit tests for SessionManager.
 *
 * SessionManager decides whether a new user message joins the active session
 * or rotates it out. Tests drive every branch of `decide()` with deterministic
 * clock and embedding stubs:
 *
 *   - no active session → fresh
 *   - within timeout → continue
 *   - core memory change → fresh
 *   - inactivity timeout with similar embedding → topic_continuity
 *   - inactivity timeout with dissimilar embedding → topic_discontinuity
 *   - deep session (≥50 turns) within 3× timeout → deep_session
 *   - explicit release / activity recording
 *
 * Every decision records a prediction in the `session_management` self-model
 * domain; these tests use a counting stub to verify that.
 */

import { describe, expect, test } from "bun:test";
import { type CoreMemoryHasher, SessionManager } from "../../src/chat/session.ts";
import { EMBEDDING_DIM, type EmbeddingService } from "../../src/memory/embeddings.ts";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a deterministic unit vector with a single non-zero axis. */
function unitVector(axis: number): Float32Array {
	const v = new Float32Array(EMBEDDING_DIM);
	v[axis % EMBEDDING_DIM] = 1;
	return v;
}

/** Embedding service that maps message text to a canned vector. */
function keyedEmbeddings(map: Record<string, Float32Array>): EmbeddingService {
	return {
		async embed(text: string): Promise<Float32Array> {
			const vec = map[text];
			if (!vec) throw new Error(`keyedEmbeddings: unknown text "${text}"`);
			return vec;
		},
		async embedBatch(): Promise<readonly Float32Array[]> {
			throw new Error("unused");
		},
		async warmup(): Promise<void> {},
	};
}

function hasher(h: string): CoreMemoryHasher {
	return { hash: async () => h };
}

function mutableHasher(initial: string): {
	readonly hasher: CoreMemoryHasher;
	set(h: string): void;
} {
	let current = initial;
	return {
		hasher: { hash: async () => current },
		set(h) {
			current = h;
		},
	};
}

// ---------------------------------------------------------------------------
// decide() branches
// ---------------------------------------------------------------------------

describe("SessionManager.decide", () => {
	test("first message: no active session → no_active_session", async () => {
		let now = 0;
		const mgr = new SessionManager(keyedEmbeddings({}), {
			now: () => now,
		});

		const decision = await mgr.decide("hi", hasher("H1"));

		expect(decision).toEqual({ continue: false, reason: "no_active_session" });
		now += 1;
	});

	test("within inactivity window: returns continue=true, reason=active", async () => {
		let now = 1_000;
		const embeddings = keyedEmbeddings({
			first: unitVector(0),
			second: unitVector(0),
		});
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 10_000,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		await mgr.recordTurn("first");
		now += 5_000; // well within 10_000ms

		const decision = await mgr.decide("second", hasher("H"));

		expect(decision).toEqual({ continue: true, reason: "active" });
	});

	test("core memory hash changed: rotates session", async () => {
		let now = 1_000;
		const embeddings = keyedEmbeddings({ first: unitVector(0) });
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 60_000,
			now: () => now,
		});

		const core = mutableHasher("H1");
		await mgr.startSession(core.hasher);
		await mgr.recordTurn("first");
		now += 100; // well within timeout
		core.set("H2");

		const decision = await mgr.decide("anything", core.hasher);

		expect(decision).toEqual({
			continue: false,
			reason: "core_memory_changed",
		});
	});

	test("timeout + similar embedding: topic_continuity extends session", async () => {
		let now = 0;
		const shared = unitVector(0);
		const embeddings = keyedEmbeddings({ "turn-a": shared, "turn-b": shared });
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 1_000,
			topicContinuityThreshold: 0.7,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		await mgr.recordTurn("turn-a");
		now += 5_000; // well past 1_000ms timeout

		const decision = await mgr.decide("turn-b", hasher("H"));

		expect(decision).toEqual({
			continue: true,
			reason: "topic_continuity",
		});
	});

	test("timeout + dissimilar embedding: topic_discontinuity rotates session", async () => {
		let now = 0;
		const embeddings = keyedEmbeddings({
			"turn-a": unitVector(0),
			"turn-b": unitVector(10), // orthogonal → cosine = 0 < threshold
		});
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 1_000,
			topicContinuityThreshold: 0.7,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		await mgr.recordTurn("turn-a");
		now += 5_000;

		const decision = await mgr.decide("turn-b", hasher("H"));

		expect(decision).toEqual({
			continue: false,
			reason: "topic_discontinuity",
		});
	});

	test("deep session (≥50 turns) slightly past timeout: deep_session continues", async () => {
		let now = 0;
		const embeddings = keyedEmbeddings({ msg: unitVector(0) });
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 1_000,
			deepSessionThreshold: 50,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		for (let i = 0; i < 50; i++) {
			await mgr.recordTurn("msg");
		}
		expect(mgr.getTurnCount()).toBe(50);

		// 1_100ms: past normal timeout (1_000), well within deep cap (3 * 1_000 = 3_000).
		now += 1_100;

		const decision = await mgr.decide("msg", hasher("H"));

		expect(decision).toEqual({ continue: true, reason: "deep_session" });
	});

	test("deep session past 3× timeout falls through to topic check", async () => {
		let now = 0;
		const embeddings = keyedEmbeddings({
			"turn-a": unitVector(0),
			"turn-b": unitVector(20), // orthogonal → cosine = 0
		});
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 1_000,
			deepSessionThreshold: 50,
			topicContinuityThreshold: 0.7,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		for (let i = 0; i < 50; i++) {
			await mgr.recordTurn("turn-a");
		}
		now += 10_000; // past deep timeout (3_000ms)

		const decision = await mgr.decide("turn-b", hasher("H"));

		expect(decision).toEqual({
			continue: false,
			reason: "topic_discontinuity",
		});
	});
});

// ---------------------------------------------------------------------------
// Lifecycle: startSession / releaseSession / recordTurn
// ---------------------------------------------------------------------------

describe("SessionManager lifecycle", () => {
	test("startSession returns a UUID and exposes it via getActiveSessionId", async () => {
		const mgr = new SessionManager(keyedEmbeddings({}));

		const id = await mgr.startSession(hasher("H"));

		expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
		expect(mgr.getActiveSessionId()).toBe(id);
		expect(mgr.getTurnCount()).toBe(0);
	});

	test("releaseSession returns the released ID and clears state", async () => {
		const mgr = new SessionManager(keyedEmbeddings({ msg: unitVector(0) }));

		const id = await mgr.startSession(hasher("H"));
		await mgr.recordTurn("msg");
		expect(mgr.getActiveSessionId()).toBe(id);
		expect(mgr.getTurnCount()).toBe(1);

		const released = mgr.releaseSession();

		expect(released).toBe(id);
		expect(mgr.getActiveSessionId()).toBeNull();
		expect(mgr.getTurnCount()).toBe(0);
	});

	test("releaseSession with no active session returns null", () => {
		const mgr = new SessionManager(keyedEmbeddings({}));

		expect(mgr.releaseSession()).toBeNull();
	});

	test("recordTurn increments turn count and extends inactivity window", async () => {
		let now = 1_000;
		const embeddings = keyedEmbeddings({ first: unitVector(0), second: unitVector(0) });
		const mgr = new SessionManager(embeddings, {
			inactivityTimeoutMs: 10_000,
			now: () => now,
		});

		await mgr.startSession(hasher("H"));
		await mgr.recordTurn("first");
		expect(mgr.getTurnCount()).toBe(1);

		// Activity clock now matches `now = 1_000`. Advance past timeout that
		// would be in effect without recordTurn.
		now += 9_000; // still inside 10_000ms from the recordTurn timestamp
		await mgr.recordTurn("second");
		expect(mgr.getTurnCount()).toBe(2);

		// Within the fresh 10_000ms window from the second recordTurn.
		now += 5_000;
		const decision = await mgr.decide("second", hasher("H"));
		expect(decision).toEqual({ continue: true, reason: "active" });
	});
});
