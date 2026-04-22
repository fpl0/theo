/**
 * Source parser tests — GitHub, Linear, email.
 *
 * Unit tests for the pure parser functions. Invalid shapes (missing
 * required headers, non-object body, non-string delivery ids) return null;
 * the server returns 400 for null parses.
 */

import { describe, expect, test } from "bun:test";
import { parseEmailPayload } from "../../../src/gates/webhooks/sources/email.ts";
import { parseGithubPayload } from "../../../src/gates/webhooks/sources/github.ts";
import { parseLinearPayload } from "../../../src/gates/webhooks/sources/linear.ts";

describe("github parser", () => {
	test("requires the delivery header", () => {
		expect(parseGithubPayload({ action: "opened" }, {})).toBeNull();
	});

	test("parses a PR-opened payload", () => {
		const parsed = parseGithubPayload(
			{
				action: "opened",
				pull_request: { title: "Add widget", html_url: "https://x" },
				repository: { full_name: "foo/bar" },
				sender: { login: "alice" },
			},
			{ "x-github-delivery": "delivery-1", "x-github-event": "pull_request" },
		);
		expect(parsed).not.toBeNull();
		expect(parsed?.source).toBe("github");
		expect(parsed?.deliveryId).toBe("delivery-1");
		expect(parsed?.autonomyDomain).toBe("code.review");
		expect(parsed?.summary).toContain("Add widget");
	});

	test("caps comment bodies to prevent runaway injection text", () => {
		const long = "A".repeat(2000);
		const parsed = parseGithubPayload(
			{
				action: "created",
				comment: { body: long },
				repository: { full_name: "x/y" },
			},
			{ "x-github-delivery": "d", "x-github-event": "issue_comment" },
		);
		// Summary should be bounded well under 2000.
		expect(parsed?.summary.length ?? 0).toBeLessThan(1100);
	});

	test("issue_comment maps to issues.triage domain", () => {
		const parsed = parseGithubPayload(
			{ action: "created", repository: { full_name: "x/y" } },
			{ "x-github-delivery": "d", "x-github-event": "issue_comment" },
		);
		expect(parsed?.autonomyDomain).toBe("issues.triage");
	});
});

describe("parseLinearPayload", () => {
	test("requires the delivery header", () => {
		expect(parseLinearPayload({ action: "create" }, {})).toBeNull();
	});

	test("parses a Linear issue create", () => {
		const parsed = parseLinearPayload(
			{
				action: "create",
				type: "Issue",
				data: { title: "Bug report", body: "Steps to reproduce" },
			},
			{ "linear-delivery": "linear-1" },
		);
		expect(parsed?.source).toBe("linear");
		expect(parsed?.deliveryId).toBe("linear-1");
		expect(parsed?.autonomyDomain).toBe("issues.triage");
		expect(parsed?.summary).toContain("Bug report");
	});

	test("caps body to 800 chars", () => {
		const huge = "X".repeat(5000);
		const parsed = parseLinearPayload(
			{ action: "create", type: "Issue", data: { title: "T", body: huge } },
			{ "linear-delivery": "l" },
		);
		expect(parsed?.summary.length ?? 0).toBeLessThan(900);
	});
});

describe("parseEmailPayload", () => {
	test("requires a delivery or messageId", () => {
		expect(parseEmailPayload({ from: "a@b" }, {})).toBeNull();
	});

	test("uses messageId from body when header missing", () => {
		const parsed = parseEmailPayload(
			{
				messageId: "msg-1",
				from: "user@example.com",
				subject: "Hi",
				bodyText: "Hello Theo",
			},
			{},
		);
		expect(parsed?.source).toBe("email");
		expect(parsed?.deliveryId).toBe("msg-1");
		expect(parsed?.summary).toContain("user@example.com");
	});
});
