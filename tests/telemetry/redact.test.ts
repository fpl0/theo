/**
 * Attribute allowlist + coarsening tests.
 *
 * Sensitive tokens never appear in redacted output; `db.statement` is
 * coarsened to operation + table; `redactions_total` increments when a
 * disallowed key is seen.
 */

import { describe, expect, test } from "bun:test";
import {
	coarsenDbStatement,
	isAllowed,
	REDACTED,
	redactAttributes,
} from "../../src/telemetry/redact.ts";

describe("redaction allowlist", () => {
	test("permits semconv keys", () => {
		expect(isAllowed("db.system")).toBe(true);
		expect(isAllowed("service.name")).toBe(true);
		expect(isAllowed("host.os.type")).toBe(true);
		expect(isAllowed("code.function")).toBe(true);
	});

	test("permits theo.* identifiers and metrics but NOT content", () => {
		expect(isAllowed("theo.gate")).toBe(true);
		expect(isAllowed("theo.model")).toBe(true);
		expect(isAllowed("theo.message.length")).toBe(true);
		expect(isAllowed("theo.message.body")).toBe(false);
		expect(isAllowed("theo.tool.arguments")).toBe(false);
	});

	test("redacts disallowed keys and calls the sink once per key", () => {
		const rejected: string[] = [];
		const out = redactAttributes(
			{
				"db.operation": "SELECT",
				"user.email": "owner@example.com",
				"theo.message.body": "hello",
			},
			(k) => rejected.push(k),
		);
		expect(out["db.operation"]).toBe("SELECT");
		expect(out["user.email"]).toBe(REDACTED);
		expect(out["theo.message.body"]).toBe(REDACTED);
		expect(rejected).toEqual(["user.email", "theo.message.body"]);
	});

	test("does NOT leak a sensitive token anywhere in exported values", () => {
		const secret = "secret123-owner@example.com";
		const out = redactAttributes({
			"user.payload": secret,
			note: `contains ${secret}`,
		});
		for (const v of Object.values(out)) {
			expect(String(v)).not.toContain(secret);
		}
	});

	test("coarsens db.statement to operation + table", () => {
		const attrs = redactAttributes({
			"db.operation": "SELECT",
			"db.statement": "SELECT * FROM node WHERE id = 42",
		});
		expect(attrs["db.statement"]).toBe("SELECT FROM node");
	});

	test("coarsenDbStatement handles insert/update/delete", () => {
		expect(coarsenDbStatement("INSERT INTO goal (id) VALUES ($1)")).toBe("INSERT FROM goal");
		expect(coarsenDbStatement("UPDATE node SET kind = $1 WHERE id = $2")).toBe("UPDATE FROM node");
		expect(coarsenDbStatement("DELETE FROM edge WHERE id = $1")).toBe("DELETE FROM edge");
	});
});
