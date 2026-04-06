/**
 * Tests for the upcaster registry: registration, chain execution, gap detection,
 * and CURRENT_VERSIONS initialization.
 */

import { beforeEach, describe, expect, test } from "bun:test";
import { ALL_EVENT_TYPES } from "../../src/events/types.ts";
import type { UpcasterRegistry } from "../../src/events/upcasters.ts";
import { createUpcasterRegistry } from "../../src/events/upcasters.ts";

describe("UpcasterRegistry", () => {
	let registry: UpcasterRegistry;

	beforeEach(() => {
		registry = createUpcasterRegistry();
	});

	test("CURRENT_VERSIONS initialized with all event types at version 1", () => {
		const versions = registry.currentVersions;
		for (const eventType of ALL_EVENT_TYPES) {
			expect(versions.get(eventType)).toBe(1);
		}
		// Verify the map contains exactly the right number of entries
		expect(versions.size).toBe(ALL_EVENT_TYPES.length);
	});

	test("single upcaster -- v1 data transformed to v2", () => {
		registry.register("turn.completed", 1, (data) => ({
			...data,
			newField: "added-in-v2",
		}));

		// currentVersions updated to 2
		expect(registry.currentVersions.get("turn.completed")).toBe(2);

		// Upcast from v1
		const result = registry.upcast("turn.completed", 1, {
			responseBody: "hello",
			durationMs: 100,
			tokensUsed: 50,
		});

		expect(result).toEqual({
			responseBody: "hello",
			durationMs: 100,
			tokensUsed: 50,
			newField: "added-in-v2",
		});
	});

	test("chain upcaster -- v1 data transformed through v2 to v3", () => {
		// Register 1->2: add field
		registry.register("turn.completed", 1, (data) => ({
			...data,
			addedInV2: true,
		}));

		// Register 2->3: rename field
		registry.register("turn.completed", 2, (data) => {
			const { tokensUsed, ...rest } = data;
			return { ...rest, tokenCount: tokensUsed };
		});

		// currentVersions updated to 3
		expect(registry.currentVersions.get("turn.completed")).toBe(3);

		// Upcast from v1 -- both transforms apply
		const result = registry.upcast("turn.completed", 1, {
			responseBody: "hello",
			durationMs: 100,
			tokensUsed: 50,
		});

		expect(result).toEqual({
			responseBody: "hello",
			durationMs: 100,
			addedInV2: true,
			tokenCount: 50,
		});
	});

	test("no upcaster needed -- data at current version returned unchanged", () => {
		// turn.completed starts at version 1, no upcasters registered
		const input = { responseBody: "hello", durationMs: 100, tokensUsed: 50 };
		const result = registry.upcast("turn.completed", 1, input);

		// Same data returned (fromVersion === currentVersion)
		expect(result).toEqual(input);
	});

	test("missing chain link -- validate() detects gap", () => {
		// Register 1->2 and 3->4, skip 2->3
		registry.register("turn.completed", 1, (data) => ({ ...data, v2: true }));
		registry.register("turn.completed", 3, (data) => ({ ...data, v4: true }));

		// currentVersions should be 4 (highest target)
		expect(registry.currentVersions.get("turn.completed")).toBe(4);

		// validate() should detect missing version 2 (the 2->3 step)
		const gaps = registry.validate();
		expect(gaps.length).toBe(1);
		expect(gaps[0]).toEqual({ eventType: "turn.completed", missingVersion: 2 });
	});

	test("unknown event type -- returns data as-is", () => {
		const input = { someField: "value" };
		const result = registry.upcast("completely.unknown.type", 1, input);

		// No upcaster, unknown type -- data returned unchanged
		expect(result).toEqual(input);
	});

	test("register updates currentVersions", () => {
		// Before: version 1
		expect(registry.currentVersions.get("turn.completed")).toBe(1);

		// Register v1->v2
		registry.register("turn.completed", 1, (data) => data);

		// After: version 2
		expect(registry.currentVersions.get("turn.completed")).toBe(2);
	});

	test("validate returns empty array when all chains are contiguous", () => {
		// Register a contiguous chain: 1->2, 2->3
		registry.register("job.created", 1, (data) => ({ ...data, v2: true }));
		registry.register("job.created", 2, (data) => ({ ...data, v3: true }));

		const gaps = registry.validate();
		expect(gaps.length).toBe(0);
	});

	test("validate returns empty array with no upcasters registered", () => {
		// No upcasters -- all types at version 1, no chains to validate
		const gaps = registry.validate();
		expect(gaps.length).toBe(0);
	});

	test("multiple event types with independent chains", () => {
		registry.register("turn.completed", 1, (data) => ({ ...data, tc2: true }));
		registry.register("job.created", 1, (data) => ({ ...data, jc2: true }));
		registry.register("job.created", 2, (data) => ({ ...data, jc3: true }));

		expect(registry.currentVersions.get("turn.completed")).toBe(2);
		expect(registry.currentVersions.get("job.created")).toBe(3);

		// Upcast turn.completed from v1
		const tcResult = registry.upcast("turn.completed", 1, { original: true });
		expect(tcResult).toEqual({ original: true, tc2: true });

		// Upcast job.created from v1
		const jcResult = registry.upcast("job.created", 1, { original: true });
		expect(jcResult).toEqual({ original: true, jc2: true, jc3: true });
	});

	test("upcast from intermediate version applies only remaining transforms", () => {
		registry.register("turn.completed", 1, (data) => ({ ...data, v2: true }));
		registry.register("turn.completed", 2, (data) => ({ ...data, v3: true }));
		registry.register("turn.completed", 3, (data) => ({ ...data, v4: true }));

		// Upcast from v2 -- should only apply 2->3 and 3->4
		const result = registry.upcast("turn.completed", 2, { original: true });
		expect(result).toEqual({ original: true, v3: true, v4: true });
	});
});
