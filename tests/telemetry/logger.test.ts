/**
 * TheoLogger — JSON output, level filtering, trace correlation, filename
 * rotation boundary.
 */

import { describe, expect, test } from "bun:test";
import { logFilePath, shouldEmit, TheoLogger } from "../../src/telemetry/logger.ts";

function takeLine(lines: readonly string[], i = 0): Record<string, unknown> {
	const line = lines[i];
	if (line === undefined) throw new Error(`no log line at index ${i}`);
	return JSON.parse(line) as Record<string, unknown>;
}

describe("TheoLogger", () => {
	test("emits a single JSON line per call at info level", () => {
		const lines: string[] = [];
		const logger = new TheoLogger({
			stdoutSink: (line) => lines.push(line),
			now: () => new Date("2026-04-21T10:00:00Z"),
		});
		logger.info("hello", { "theo.gate": "cli.owner" });
		expect(lines).toHaveLength(1);
		const parsed = takeLine(lines);
		expect(parsed).toMatchObject({
			level: "info",
			message: "hello",
			component: "theo",
			timestamp: "2026-04-21T10:00:00.000Z",
			attributes: { "theo.gate": "cli.owner" },
		});
	});

	test("debug below info level is dropped", () => {
		const lines: string[] = [];
		const logger = new TheoLogger({ level: "info", stdoutSink: (l) => lines.push(l) });
		logger.debug("quiet");
		logger.info("loud");
		expect(lines).toHaveLength(1);
	});

	test("passes attributes through unchanged (redaction disabled)", () => {
		const lines: string[] = [];
		const logger = new TheoLogger({ stdoutSink: (l) => lines.push(l) });
		logger.info("event", { "user.secret": "top-secret", "theo.gate": "cli.owner" });
		const parsed = takeLine(lines);
		const attrs = parsed["attributes"] as Record<string, unknown>;
		expect(attrs["user.secret"]).toBe("top-secret");
		expect(attrs["theo.gate"]).toBe("cli.owner");
	});

	test("attaches active trace context when provided", () => {
		const lines: string[] = [];
		const logger = new TheoLogger({
			stdoutSink: (l) => lines.push(l),
			activeContext: () => ({ traceId: "abc", spanId: "def" }),
		});
		logger.info("test");
		const parsed = takeLine(lines);
		expect(parsed["traceId"]).toBe("abc");
		expect(parsed["spanId"]).toBe("def");
	});

	test("shouldEmit honors level ordering", () => {
		expect(shouldEmit("info", "debug")).toBe(false);
		expect(shouldEmit("info", "warn")).toBe(true);
		expect(shouldEmit("error", "warn")).toBe(false);
	});

	test("log file path rotates by day", () => {
		const a = logFilePath("/tmp/logs", new Date("2026-04-21T23:00:00Z"));
		const b = logFilePath("/tmp/logs", new Date("2026-04-22T00:01:00Z"));
		expect(a).toBe("/tmp/logs/theo-2026-04-21.log");
		expect(b).toBe("/tmp/logs/theo-2026-04-22.log");
	});
});
