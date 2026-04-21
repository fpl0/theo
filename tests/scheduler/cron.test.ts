/**
 * Unit tests for the cron wrapper.
 *
 * Every test expresses the "from" instant in UTC — cron-parser defaults to
 * UTC, so this keeps expectations timezone-invariant across machines.
 */

import { describe, expect, test } from "bun:test";
import { isValidCron, nextRun } from "../../src/scheduler/cron.ts";

describe("nextRun", () => {
	test("every minute advances to the next minute boundary", () => {
		const from = new Date(Date.UTC(2026, 0, 15, 10, 30, 15));
		const next = nextRun("* * * * *", from);
		expect(next.toISOString()).toBe("2026-01-15T10:31:00.000Z");
	});

	test("every hour advances to the next top-of-hour", () => {
		const from = new Date(Date.UTC(2026, 0, 15, 10, 30, 0));
		const next = nextRun("0 * * * *", from);
		expect(next.toISOString()).toBe("2026-01-15T11:00:00.000Z");
	});

	test("every 6 hours advances to 12:00 from 10:00", () => {
		const from = new Date(Date.UTC(2026, 0, 15, 10, 0, 0));
		const next = nextRun("0 */6 * * *", from);
		expect(next.toISOString()).toBe("2026-01-15T12:00:00.000Z");
	});

	test("weekdays 9am jumps Friday 10:00 to Monday 09:00", () => {
		// 2026-01-16 is a Friday.
		const from = new Date(Date.UTC(2026, 0, 16, 10, 0, 0));
		const next = nextRun("0 9 * * 1-5", from);
		expect(next.toISOString()).toBe("2026-01-19T09:00:00.000Z");
	});

	test("weekly Sunday 3am from Monday lands on next Sunday 03:00", () => {
		// 2026-01-19 is a Monday.
		const from = new Date(Date.UTC(2026, 0, 19, 12, 0, 0));
		const next = nextRun("0 3 * * 0", from);
		expect(next.toISOString()).toBe("2026-01-25T03:00:00.000Z");
	});

	test("exact-match next_run_at advances past the current instant", () => {
		// When `from` is itself a match, cron-parser's `next()` returns the
		// following match, not the current one. This is the behavior the tick
		// loop relies on — otherwise `updateNextRun(nextRun(cron, now))` would
		// leave the job due immediately.
		const from = new Date(Date.UTC(2026, 0, 15, 12, 0, 0));
		const next = nextRun("0 */6 * * *", from);
		expect(next.toISOString()).toBe("2026-01-15T18:00:00.000Z");
	});

	test("malformed expression throws", () => {
		expect(() => nextRun("not a cron", new Date())).toThrow();
	});
});

describe("isValidCron", () => {
	test("accepts standard 5-field expressions", () => {
		expect(isValidCron("* * * * *")).toBe(true);
		expect(isValidCron("0 */6 * * *")).toBe(true);
		expect(isValidCron("0 9 * * 1-5")).toBe(true);
	});

	test("rejects malformed expressions", () => {
		// cron-parser is lenient on short expressions (it pads with defaults)
		// so the cases we can reliably reject are values outside any field's
		// numeric range and non-cron text.
		expect(isValidCron("not a cron")).toBe(false);
		expect(isValidCron("99 99 99 99 99")).toBe(false);
		expect(isValidCron("0 0 0 13 *")).toBe(false); // month=13
	});
});
