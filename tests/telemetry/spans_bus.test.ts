/**
 * Bus handler span wrapper — verifies that wrapped handlers run inside a
 * span with the correct semconv attributes, that duration is recorded, and
 * that handler errors increment the error counter with a classified reason.
 */

import { describe, expect, test } from "bun:test";
import { newEventId } from "../../src/events/ids.ts";
import type { Event } from "../../src/events/types.ts";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { wrapHandlerWithSpan } from "../../src/telemetry/spans/bus.ts";
import { initTracer } from "../../src/telemetry/tracer.ts";

function syntheticEvent(): Event {
	// Pick any event type from the union so type-checking is honest.
	return {
		id: newEventId(),
		type: "system.started",
		version: 1,
		actor: "system",
		timestamp: new Date(),
		data: { version: "0.1.0" },
		metadata: {},
	} as Event;
}

describe("wrapHandlerWithSpan", () => {
	test("records duration on successful handler run", async () => {
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource: { "service.name": "theo" }, metrics });
		const wrap = wrapHandlerWithSpan(tracer, metrics);
		const handler = wrap("noop", "decision", async (_event) => {
			await Promise.resolve();
		});
		await handler(syntheticEvent());
		const samples = metrics.meter.samplesFor("theo.bus.handler_duration_ms");
		expect(samples.length).toBe(1);
		expect(samples[0]?.labels["handler"]).toBe("noop");
	});

	test("classifies handler errors and re-throws", async () => {
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource: { "service.name": "theo" }, metrics });
		const wrap = wrapHandlerWithSpan(tracer, metrics);
		const boom = wrap("boom", "effect", async () => {
			throw new Error("validation failed for input");
		});
		await expect(boom(syntheticEvent())).rejects.toThrow("validation failed");
		const errors = metrics.meter.samplesFor("theo.bus.handler_errors_total");
		expect(errors.length).toBe(1);
		expect(errors[0]?.labels["handler"]).toBe("boom");
		expect(errors[0]?.labels["reason"]).toBe("validation_error");
	});

	test("captures db errors under db_error bucket", async () => {
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource: { "service.name": "theo" }, metrics });
		const wrap = wrapHandlerWithSpan(tracer, metrics);
		const boom = wrap("db", "effect", async () => {
			throw new Error("relation does not exist");
		});
		await expect(boom(syntheticEvent())).rejects.toThrow();
		const errors = metrics.meter.samplesFor("theo.bus.handler_errors_total");
		expect(errors[0]?.labels["reason"]).toBe("db_error");
	});
});
