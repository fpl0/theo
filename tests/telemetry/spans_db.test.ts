/**
 * Postgres query-timing instrumentation test.
 *
 * Uses a synthetic postgres.js `sql` proxy to exercise `instrumentSql`
 * without a live database. The assertion is that `theo.db.query_duration_ms`
 * records a sample with the right `operation` and `table` labels.
 */

import { describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { instrumentSql } from "../../src/telemetry/spans/db.ts";

function makeFakeSql(resolveValue: unknown): Sql {
	const target = (..._args: unknown[]): Promise<unknown> => Promise.resolve(resolveValue);
	// Add dummy properties so consumers don't blow up on `sql.begin`.
	Object.defineProperty(target, "begin", { value: () => Promise.resolve(), enumerable: false });
	return target as unknown as Sql;
}

describe("instrumentSql", () => {
	test("records duration and operation for a SELECT query", async () => {
		const metrics = initMetrics({ environment: "test" });
		const sql = makeFakeSql([{ n: 1 }]);
		const wrapped = instrumentSql(sql, metrics);

		const strings = ["SELECT 1 FROM node WHERE id = ", ""] as unknown as TemplateStringsArray;
		(strings as unknown as { raw: string[] }).raw = ["SELECT 1 FROM node WHERE id = ", ""];
		const result = await (
			wrapped as unknown as (s: TemplateStringsArray, ...v: unknown[]) => Promise<unknown>
		)(strings, 42);
		expect(result).toEqual([{ n: 1 }]);

		// Allow the .then handler to flush.
		await new Promise<void>((resolve) => setImmediate(resolve));

		const samples = metrics.meter.samplesFor("theo.db.query_duration_ms");
		expect(samples.length).toBe(1);
		const sample = samples[0];
		expect(sample?.labels["operation"]).toBe("SELECT");
		expect(sample?.labels["table"]).toBe("node");
		expect(sample?.labels["status"]).toBe("ok");
	});

	test("records status=failed when the query rejects", async () => {
		const metrics = initMetrics({ environment: "test" });
		const badSql = ((..._args: unknown[]): Promise<unknown> =>
			Promise.reject(new Error("boom"))) as unknown as Sql;
		const wrapped = instrumentSql(badSql, metrics);

		const strings = ["INSERT INTO node (body) VALUES (", ")"] as unknown as TemplateStringsArray;
		(strings as unknown as { raw: string[] }).raw = ["INSERT INTO node (body) VALUES (", ")"];
		try {
			await (wrapped as unknown as (s: TemplateStringsArray, ...v: unknown[]) => Promise<unknown>)(
				strings,
				"hello",
			);
		} catch {
			// expected
		}

		await new Promise<void>((resolve) => setImmediate(resolve));
		const samples = metrics.meter.samplesFor("theo.db.query_duration_ms");
		expect(samples.length).toBe(1);
		expect(samples[0]?.labels["status"]).toBe("failed");
		expect(samples[0]?.labels["operation"]).toBe("INSERT");
	});

	test("non-tagged-template calls pass through untimed", async () => {
		const metrics = initMetrics({ environment: "test" });
		const sql = makeFakeSql([{ n: 1 }]);
		const wrapped = instrumentSql(sql, metrics);

		// Calling with a plain array (not a TemplateStringsArray) simulates
		// postgres.js internal invocations; we must not time those.
		const plain = ["SELECT 1"] as unknown as TemplateStringsArray;
		// No `raw` property — isTemplateStringsArray returns false.
		const result = await (
			wrapped as unknown as (s: TemplateStringsArray, ...v: unknown[]) => Promise<unknown>
		)(plain);
		expect(result).toEqual([{ n: 1 }]);
		await new Promise<void>((resolve) => setImmediate(resolve));
		expect(metrics.meter.samplesFor("theo.db.query_duration_ms").length).toBe(0);
	});
});
