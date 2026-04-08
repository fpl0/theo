/**
 * Unit tests for bootstrap identity and onboarding detection.
 *
 * Tests bootstrapIdentity() idempotency, seed content validation,
 * CoreMemoryRepository interaction, error handling, and onboarding
 * detection logic.
 *
 * Pure unit tests — CoreMemoryRepository is mocked, no database needed.
 */

import { describe, expect, mock, test } from "bun:test";
import {
	bootstrapIdentity,
	INITIAL_GOALS,
	INITIAL_PERSONA,
	ONBOARDING_PREAMBLE,
	shouldAugmentForOnboarding,
} from "../../src/chat/bootstrap.ts";
import type { CoreMemoryRepository } from "../../src/memory/core.ts";
import type { JsonValue } from "../../src/memory/types.ts";
import { SlotNotFoundError } from "../../src/memory/types.ts";

// ---------------------------------------------------------------------------
// Mock CoreMemoryRepository
// ---------------------------------------------------------------------------

interface MockCoreMemory {
	readonly readSlot: ReturnType<typeof mock>;
	readonly update: ReturnType<typeof mock>;
}

function createMockCoreMemory(personaBody: JsonValue = {}): MockCoreMemory & CoreMemoryRepository {
	const readSlot = mock((slot: string) => {
		if (slot === "persona") {
			return Promise.resolve({ ok: true as const, value: personaBody });
		}
		return Promise.resolve({ ok: true as const, value: {} });
	});

	const update = mock(() => Promise.resolve());

	// Cast to satisfy the interface while keeping mock access
	return { readSlot, update } as unknown as MockCoreMemory & CoreMemoryRepository;
}

function createFailingMockCoreMemory(): MockCoreMemory & CoreMemoryRepository {
	const readSlot = mock(() =>
		Promise.resolve({
			ok: false as const,
			error: new SlotNotFoundError("persona"),
		}),
	);

	const update = mock(() => Promise.resolve());

	return { readSlot, update } as unknown as MockCoreMemory & CoreMemoryRepository;
}

// ---------------------------------------------------------------------------
// bootstrapIdentity
// ---------------------------------------------------------------------------

describe("bootstrapIdentity", () => {
	test("first bootstrap: seeds persona and goals when persona is empty", async () => {
		const repo = createMockCoreMemory({});

		const result = await bootstrapIdentity(repo);

		expect(result.seeded).toBe(true);
		expect(repo.update).toHaveBeenCalledTimes(2);

		// Promise.all — order is not guaranteed, so check set membership
		const calls = repo.update.mock.calls.map((c: unknown[]) => [c[0], c[1], c[2]]);
		expect(calls).toContainEqual(["persona", INITIAL_PERSONA, "system"]);
		expect(calls).toContainEqual(["goals", INITIAL_GOALS, "system"]);
	});

	test("idempotent: returns seeded=false when persona is already populated", async () => {
		const repo = createMockCoreMemory({ name: "Theo", voice: { tone: "warm" } });

		const result = await bootstrapIdentity(repo);

		expect(result.seeded).toBe(false);
		expect(repo.update).not.toHaveBeenCalled();
	});

	test("uses CoreMemoryRepository.update with actor 'system'", async () => {
		const repo = createMockCoreMemory({});

		await bootstrapIdentity(repo);

		// Verify both calls use 'system' as actor
		for (const call of repo.update.mock.calls) {
			expect(call[2]).toBe("system");
		}
	});

	test("throws when persona slot is missing (database corruption)", async () => {
		const repo = createFailingMockCoreMemory();

		await expect(bootstrapIdentity(repo)).rejects.toThrow("Core memory slot 'persona' not found");
	});

	test("propagates error when update fails", async () => {
		const repo = createMockCoreMemory({});
		repo.update = mock(() => Promise.reject(new Error("connection lost")));

		await expect(bootstrapIdentity(repo)).rejects.toThrow("connection lost");
	});

	test("persona seed contains required structure", () => {
		const persona = INITIAL_PERSONA as Record<string, JsonValue>;

		expect(persona["name"]).toBe("Theo");
		expect(persona["voice"]).toBeDefined();
		expect(persona["autonomy"]).toBeDefined();
		expect(persona["memory_philosophy"]).toBeDefined();

		// Voice structure
		const voice = persona["voice"] as Record<string, JsonValue>;
		expect(voice["tone"]).toBeDefined();
		expect(voice["style"]).toBeDefined();
		expect(Array.isArray(voice["avoids"])).toBe(true);
		expect(Array.isArray(voice["qualities"])).toBe(true);

		// Autonomy structure
		const autonomy = persona["autonomy"] as Record<string, JsonValue>;
		expect(autonomy["default_level"]).toBe("suggest");
		expect(Array.isArray(autonomy["levels"])).toBe(true);
	});

	test("goals seed contains three prioritized goals", () => {
		const goals = INITIAL_GOALS as Record<string, JsonValue>;

		expect(goals["primary"]).toBeDefined();
		expect(goals["secondary"]).toBeDefined();
		expect(goals["tertiary"]).toBeDefined();

		// Each goal has description and status
		for (const key of ["primary", "secondary", "tertiary"]) {
			const goal = goals[key] as Record<string, JsonValue>;
			expect(goal["description"]).toBeDefined();
			expect(goal["status"]).toBeDefined();
		}
	});
});

// ---------------------------------------------------------------------------
// Onboarding augmentation detection
// ---------------------------------------------------------------------------

describe("onboarding augmentation detection", () => {
	test("returns true when user model dimensions are empty", () => {
		expect(shouldAugmentForOnboarding([])).toBe(true);
	});

	test("returns false when user model has dimensions", () => {
		expect(shouldAugmentForOnboarding([{ name: "style" }])).toBe(false);
	});

	test("returns false with multiple dimensions", () => {
		expect(
			shouldAugmentForOnboarding([{ name: "style" }, { name: "interests" }, { name: "tone" }]),
		).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// ONBOARDING_PREAMBLE
// ---------------------------------------------------------------------------

describe("ONBOARDING_PREAMBLE", () => {
	test("contains 'first conversation'", () => {
		expect(ONBOARDING_PREAMBLE).toContain("first conversation");
	});

	test("instructs agent NOT to mention 'dimensions' to the owner", () => {
		// The preamble references "dimensions" only in a "Do NOT" instruction
		// telling the agent not to expose this internal concept to the owner
		expect(ONBOARDING_PREAMBLE).toContain('Mention "dimensions"');
		expect(ONBOARDING_PREAMBLE).toContain("Do NOT:");
	});

	test("instructs agent NOT to mention 'confidence scores' to the owner", () => {
		// Same as above — "confidence scores" appears only in the "Do NOT" block
		expect(ONBOARDING_PREAMBLE).toContain('"confidence scores"');
		expect(ONBOARDING_PREAMBLE).toContain("Do NOT:");
	});

	test("instructs natural conversation, not questionnaire", () => {
		expect(ONBOARDING_PREAMBLE).toContain("not a survey");
		expect(ONBOARDING_PREAMBLE).toContain("Do NOT:");
		expect(ONBOARDING_PREAMBLE).toContain("Run through a formal questionnaire");
	});
});
