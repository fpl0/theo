/**
 * Closed-set label guard tests.
 *
 * Rejects unknown values, counts the rejection, and returns "unknown" so
 * metric cardinality stays bounded.
 */

import { beforeEach, describe, expect, test } from "bun:test";
import {
	asGate,
	asHandlerErrorReason,
	asModel,
	asProbeFailReason,
	asProposalKind,
	asRole,
	assertLabel,
	GATES,
	MODELS,
	PROBE_FAIL_REASONS,
	ROLES,
	registerCardinalityRejectSink,
} from "../../src/telemetry/labels.ts";

describe("closed-set label guard", () => {
	let rejects: Array<{ metric: string; label: string }>;
	beforeEach(() => {
		rejects = [];
		registerCardinalityRejectSink((metric, label) => {
			rejects.push({ metric, label });
		});
	});

	test("accepts known values", () => {
		expect(assertLabel(GATES, "cli.owner", "m", "gate")).toBe("cli.owner");
		expect(assertLabel(MODELS, "claude-opus-4-7", "m", "model")).toBe("claude-opus-4-7");
	});

	test("rejects unknown values and counts them", () => {
		expect(assertLabel(GATES, "unknown-gate-xyz", "theo.turns.total", "gate")).toBe("unknown");
		expect(rejects).toHaveLength(1);
		expect(rejects[0]).toMatchObject({ metric: "theo.turns.total", label: "gate" });
	});

	test("'unknown' is itself a member of every enum", () => {
		expect(GATES).toContain("unknown");
		expect(MODELS).toContain("unknown");
		expect(ROLES).toContain("unknown");
		expect(PROBE_FAIL_REASONS).toContain("unknown");
	});

	test("helper fns coerce through assertLabel", () => {
		expect(asGate("cli.owner", "m")).toBe("cli.owner");
		expect(asGate("fake.gate", "m")).toBe("unknown");
		expect(asModel("claude-haiku-4-5", "m")).toBe("claude-haiku-4-5");
		expect(asRole("executor", "m")).toBe("executor");
		expect(asHandlerErrorReason("timeout", "m")).toBe("timeout");
		expect(asProposalKind("new_goal", "m")).toBe("new_goal");
		expect(asProbeFailReason("timeout", "m")).toBe("timeout");
	});
});
