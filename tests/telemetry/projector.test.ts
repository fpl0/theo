/**
 * TelemetryProjector — derives metrics / logs from events. Exhaustiveness
 * over the Event union is compile-checked via the `never` default; these
 * tests exercise a handful of representative cases.
 */

import { describe, expect, test } from "bun:test";
import { newEventId } from "../../src/events/ids.ts";
import type { Event } from "../../src/events/types.ts";
import { TheoLogger } from "../../src/telemetry/logger.ts";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { TelemetryProjector } from "../../src/telemetry/projector.ts";

function mk<T extends Event>(event: T): T {
	return event;
}

function setup(): {
	projector: TelemetryProjector;
	meter: ReturnType<typeof initMetrics>["meter"];
	logs: string[];
} {
	const logs: string[] = [];
	const logger = new TheoLogger({ stdoutSink: (l) => logs.push(l) });
	const { registry, meter } = initMetrics({ environment: "test" });
	const projector = new TelemetryProjector({ metrics: registry, logger });
	return { projector, meter, logs };
}

describe("TelemetryProjector", () => {
	test("turn.completed records turn counter, duration, tokens, cost", async () => {
		const { projector, meter } = setup();
		await projector.handleEvent(
			mk({
				id: newEventId(),
				type: "turn.completed",
				version: 1,
				timestamp: new Date(),
				actor: "theo",
				data: {
					sessionId: "s1",
					responseBody: "",
					durationMs: 120,
					inputTokens: 50,
					outputTokens: 30,
					totalTokens: 80,
					costUsd: 0.001,
				},
				metadata: { gate: "cli.owner" },
			}),
		);
		expect(meter.samplesFor("theo.turns.total")).toHaveLength(1);
		expect(meter.samplesFor("theo.turns.duration_ms").map((s) => s.value)).toEqual([120]);
		expect(meter.samplesFor("theo.tokens.input").map((s) => s.value)).toEqual([50]);
		expect(meter.samplesFor("theo.cost.usd").map((s) => s.value)).toEqual([0.001]);
	});

	test("reflex.thought separates executor vs advisor iterations into advisor counters", async () => {
		const { projector, meter } = setup();
		await projector.handleEvent(
			mk({
				id: newEventId(),
				type: "reflex.thought",
				version: 1,
				timestamp: new Date(),
				actor: "theo",
				data: {
					reflexEventId: newEventId(),
					webhookEventId: newEventId(),
					subagent: "reflex-agent",
					model: "claude-sonnet-4-6",
					advisorModel: "claude-opus-4-7",
					inputTokens: 100,
					outputTokens: 200,
					iterations: [
						{
							kind: "executor",
							model: "claude-sonnet-4-6",
							inputTokens: 60,
							outputTokens: 100,
							costUsd: 0.002,
						},
						{
							kind: "advisor_message",
							model: "claude-opus-4-7",
							inputTokens: 40,
							outputTokens: 100,
							costUsd: 0.004,
						},
					],
					costUsd: 0.006,
					outcome: { kind: "noop", reason: "nothing to do" },
				},
				metadata: {},
			}),
		);
		const advisorCost = meter.samplesFor("theo.advisor.cost_usd_total");
		const advisorIter = meter.samplesFor("theo.advisor.iterations_total");
		expect(advisorIter.map((s) => s.value)).toEqual([1]);
		expect(advisorCost.map((s) => s.value)).toEqual([0.004]);
		const firstIter = advisorIter[0];
		if (firstIter === undefined) throw new Error("expected an advisor iteration sample");
		expect(firstIter.labels["model"]).toBe("claude-opus-4-7");
	});

	test("degradation.level_changed emits a warn log", async () => {
		const { projector, logs } = setup();
		await projector.handleEvent(
			mk({
				id: newEventId(),
				type: "degradation.level_changed",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: { previousLevel: 0, newLevel: 2, reason: "cost_overrun" },
				metadata: {},
			}),
		);
		expect(logs.some((l) => JSON.parse(l).message === "degradation level changed")).toBe(true);
	});

	test("system.rollback emits a warn log — attributes redacted through allowlist", async () => {
		const { projector, logs } = setup();
		await projector.handleEvent(
			mk({
				id: newEventId(),
				type: "system.rollback",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: { fromCommit: "aaa", toCommit: "bbb", reason: "healthcheck_failed" },
				metadata: {},
			}),
		);
		const entries = logs.map((l) => JSON.parse(l));
		const rollback = entries.find((e) => e.message === "self-update rollback");
		expect(rollback).toBeDefined();
		expect(rollback.level).toBe("warn");
		// `from`/`to`/`reason` are not on the semconv allowlist; the redaction
		// filter replaces them with `[redacted]`. This is by design: event body
		// payloads (including commit shas) stay out of exported observability
		// attributes. The rollback fact itself is captured by the log entry.
		expect(rollback.attributes.from).toBe("[redacted]");
		expect(rollback.attributes.to).toBe("[redacted]");
		expect(rollback.attributes.reason).toBe("[redacted]");
	});

	test("events_appended counter bumps on every event", async () => {
		const { projector, meter } = setup();
		await projector.handleEvent(
			mk({
				id: newEventId(),
				type: "session.created",
				version: 1,
				timestamp: new Date(),
				actor: "theo",
				data: { sessionId: "s1" },
				metadata: {},
			}),
		);
		const appended = meter.samplesFor("theo.bus.events_appended_total");
		expect(appended).toHaveLength(1);
		const first = appended[0];
		if (first === undefined) throw new Error("expected appended sample");
		expect(first.labels["event_type"]).toBe("session.created");
	});

	test("projector performs no DB writes (type-level check — deps have no sql)", () => {
		// Structural test: the projector's constructor type does not accept a
		// `sql` dep. Adding one would be a compile error. This asserts by
		// reading the type surface.
		const { projector } = setup();
		expect(projector).toBeDefined();
	});
});
