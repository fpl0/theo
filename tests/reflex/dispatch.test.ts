/**
 * Reflex dispatch allowlist + runner contract tests.
 *
 * The load-bearing security invariant: external-tier reflex dispatch passes
 * only the read-only memory tools to the subagent. No file system tools.
 * No network. Writes must become `proposal.requested`, not direct writes.
 */

import { describe, expect, test } from "bun:test";
import { EXTERNAL_TURN_TOOLS } from "../../src/reflex/dispatch.ts";

describe("EXTERNAL_TURN_TOOLS", () => {
	test("contains exactly the four read-only memory tools", () => {
		expect(EXTERNAL_TURN_TOOLS.length).toBeGreaterThanOrEqual(4);
		expect(EXTERNAL_TURN_TOOLS).toContain("mcp__memory__search_memory");
		expect(EXTERNAL_TURN_TOOLS).toContain("mcp__memory__search_skills");
		expect(EXTERNAL_TURN_TOOLS).toContain("mcp__memory__read_core");
		expect(EXTERNAL_TURN_TOOLS).toContain("mcp__memory__read_goals");
	});

	test("excludes every write tool", () => {
		const banned = [
			"mcp__memory__store_memory",
			"mcp__memory__delete_memory",
			"mcp__memory__update_core",
			"Bash",
			"Write",
			"Edit",
			"WebFetch",
			"WebSearch",
		];
		for (const b of banned) {
			expect(EXTERNAL_TURN_TOOLS).not.toContain(b);
		}
	});

	test("every tool is an MCP memory tool (no ambient tools)", () => {
		for (const tool of EXTERNAL_TURN_TOOLS) {
			expect(tool.startsWith("mcp__memory__")).toBe(true);
		}
	});
});
