/**
 * CLI operator commands — argument parsing and dispatcher behavior.
 *
 * Unit-level; database-backed behavior is exercised separately in the
 * Phase 13b integration test.
 */

import { describe, expect, test } from "bun:test";
import {
	isOperatorCommand,
	OPERATOR_COMMANDS,
	parseOperatorLine,
	runOperatorCommand,
} from "../../../src/gates/cli/operator.ts";

describe("parseOperatorLine", () => {
	test("parses name and positional args", () => {
		expect(parseOperatorLine("/approve 01H")).toEqual({ name: "/approve", args: ["01H"] });
		expect(parseOperatorLine("  /reject 01H not applicable  ")).toEqual({
			name: "/reject",
			args: ["01H", "not", "applicable"],
		});
	});

	test("returns null for non-command input", () => {
		expect(parseOperatorLine("hello")).toBeNull();
		expect(parseOperatorLine("")).toBeNull();
	});
});

describe("isOperatorCommand", () => {
	test("recognizes every declared command", () => {
		for (const name of Object.keys(OPERATOR_COMMANDS)) {
			expect(isOperatorCommand(name)).toBe(true);
		}
	});

	test("rejects unknown commands", () => {
		expect(isOperatorCommand("/status")).toBe(false); // not an OPERATOR command
		expect(isOperatorCommand("/made-up")).toBe(false);
	});
});

describe("runOperatorCommand", () => {
	test("unknown command reports an error result", async () => {
		const result = await runOperatorCommand(
			// deps never touched for unknown commands
			{ sql: null as never, bus: null as never },
			"/no-such-command",
		);
		expect(result.ok).toBe(false);
		expect(result.message).toContain("unknown command");
	});

	test("non-slash input is rejected", async () => {
		const result = await runOperatorCommand({ sql: null as never, bus: null as never }, "hello");
		expect(result.ok).toBe(false);
		expect(result.message).toBe("not a command");
	});
});
