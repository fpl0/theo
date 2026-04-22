/**
 * Workspace discipline tests.
 */

import { describe, expect, test } from "bun:test";
import {
	buildPrBody,
	proposalBranchName,
	SCRUB_PATTERNS,
	scrubEnv,
} from "../../src/proposals/workspace.ts";

describe("proposalBranchName", () => {
	test("builds theo/proposal/<id>/<slug>", () => {
		const name = proposalBranchName("abc123", "Add telescope to observatory");
		expect(name).toBe("theo/proposal/abc123/add-telescope-to-observatory");
	});
	test("sanitizes non-alphanumeric chars", () => {
		const name = proposalBranchName("id", "Fix! @ home?");
		expect(name).toMatch(/^theo\/proposal\/id\/[a-z0-9-]+$/);
		expect(name).not.toContain("!");
		expect(name).not.toContain("@");
	});
	test("empty summary falls back to 'item'", () => {
		const name = proposalBranchName("id", "");
		expect(name).toBe("theo/proposal/id/item");
	});
	test("long summary truncated to 40 chars", () => {
		const long = "a".repeat(200);
		const name = proposalBranchName("id", long);
		const slug = name.split("/").at(-1) ?? "";
		expect(slug.length).toBeLessThanOrEqual(40);
	});
});

describe("scrubEnv", () => {
	test("removes ANTHROPIC_API_KEY", () => {
		const scrubbed = scrubEnv({
			ANTHROPIC_API_KEY: "sk-ant-xxx",
			PATH: "/usr/bin",
		});
		expect(scrubbed["ANTHROPIC_API_KEY"]).toBeUndefined();
		expect(scrubbed["PATH"]).toBe("/usr/bin");
	});
	test("removes every *_KEY / *_SECRET / *_TOKEN", () => {
		const scrubbed = scrubEnv({
			OWNER_KEY: "x",
			DEPLOY_SECRET: "y",
			CI_TOKEN: "z",
			OTHER: "kept",
		});
		expect(scrubbed["OWNER_KEY"]).toBeUndefined();
		expect(scrubbed["DEPLOY_SECRET"]).toBeUndefined();
		expect(scrubbed["CI_TOKEN"]).toBeUndefined();
		expect(scrubbed["OTHER"]).toBe("kept");
	});
	test("removes DATABASE_URL", () => {
		const scrubbed = scrubEnv({ DATABASE_URL: "postgres://..." });
		expect(scrubbed["DATABASE_URL"]).toBeUndefined();
	});
	test("drops undefined values", () => {
		const scrubbed = scrubEnv({ FOO: undefined, BAR: "yes" });
		expect(scrubbed["FOO"]).toBeUndefined();
		expect(scrubbed["BAR"]).toBe("yes");
	});
	test("at least 10 scrub patterns (pattern coverage)", () => {
		expect(SCRUB_PATTERNS.length).toBeGreaterThanOrEqual(10);
	});
});

describe("buildPrBody", () => {
	test("embeds proposal id + causation + reasoning", () => {
		const body = buildPrBody({
			proposalId: "P123",
			sourceCauseId: "E1",
			originEventId: "E0",
			reasoning: "because X",
		});
		expect(body).toContain("P123");
		expect(body).toContain("E1");
		expect(body).toContain("E0");
		expect(body).toContain("because X");
		expect(body).toContain("Draft PR");
	});
});
