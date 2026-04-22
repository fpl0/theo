/**
 * Exemplar attachment test — every histogram observation recorded inside
 * an active span carries an exemplar with the span's traceId.
 *
 * The tracer publishes the active context via `registerExemplarGetter`;
 * `InMemoryMeter.histogram(...).record(...)` pulls it and tags the sample.
 * This is the glue that lets Grafana jump from a p99 outlier to the
 * originating trace.
 */

import { describe, expect, test } from "bun:test";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { initTracer } from "../../src/telemetry/tracer.ts";

describe("exemplar attachment", () => {
	test("histogram sample recorded inside a span carries traceId", async () => {
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({
			resource: { "service.name": "theo" },
			metrics,
		});

		await tracer.withSpan("turn", { theo: "gate" }, async () => {
			metrics.registry.turnDuration.record(123, { gate: "cli.owner" });
		});

		const samples = metrics.meter.samplesFor("theo.turns.duration_ms");
		expect(samples.length).toBe(1);
		const sample = samples[0];
		expect(sample?.exemplar).toBeDefined();
		expect(typeof sample?.exemplar?.traceId).toBe("string");
		expect(sample?.exemplar?.traceId.length).toBe(32);
	});

	test("histogram sample recorded outside any span has no exemplar", () => {
		const metrics = initMetrics({ environment: "test" });
		// Tracer NOT initialized — no active context getter.
		metrics.registry.turnDuration.record(123, { gate: "cli.owner" });
		const samples = metrics.meter.samplesFor("theo.turns.duration_ms");
		expect(samples.length).toBe(1);
		expect(samples[0]?.exemplar).toBeUndefined();
	});
});
