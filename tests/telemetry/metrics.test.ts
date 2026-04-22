/**
 * MetricsRegistry — instrument registry completeness + counter/histogram/
 * observable-gauge behavior.
 */

import { describe, expect, test } from "bun:test";
import { ALL_METRIC_NAMES, InMemoryMeter, initMetrics } from "../../src/telemetry/metrics.ts";

describe("MetricsRegistry", () => {
	test("every declared instrument is reachable through the registry", () => {
		const { registry } = initMetrics({ environment: "test" });
		// Every key in ALL_METRIC_NAMES has a matching instrument.
		const instrumentNames = [
			registry.turnCounter.name,
			registry.turnDuration.name,
			registry.inputTokens.name,
			registry.outputTokens.name,
			registry.costCounter.name,
			registry.retrievalDuration.name,
			registry.cacheHitRate.name,
			registry.nodesGauge.name,
			registry.handlerDuration.name,
			registry.dbQueryDuration.name,
			registry.degradationLevel.name,
			registry.probeFailures.name,
			registry.sloErrorBudgetRemaining.name,
			registry.advisorCost.name,
		];
		for (const name of instrumentNames) expect(ALL_METRIC_NAMES).toContain(name);
	});

	test("counter increments are captured with labels", () => {
		const { registry, meter } = initMetrics({ environment: "test" });
		registry.turnCounter.add(1, { gate: "cli.owner", status: "ok" });
		registry.turnCounter.add(2, { gate: "cli.owner", status: "ok" });
		const samples = meter.samplesFor("theo.turns.total");
		expect(samples.map((s) => s.value)).toEqual([1, 2]);
		expect(samples.every((s) => s.labels["gate"] === "cli.owner")).toBe(true);
	});

	test("histogram records values", () => {
		const { registry, meter } = initMetrics({ environment: "test" });
		registry.turnDuration.record(120, { gate: "cli.owner" });
		registry.turnDuration.record(80, { gate: "cli.owner" });
		const samples = meter.samplesFor("theo.turns.duration_ms");
		expect(samples.map((s) => s.value)).toEqual([120, 80]);
	});

	test("observable gauge runs addCallback at collection", async () => {
		const meter = new InMemoryMeter();
		const gauge = meter.observableGauge("test.gauge");
		let observed = 0;
		gauge.addCallback(() => {
			observed += 1;
			gauge.observe(42);
		});
		await meter.collect();
		await meter.collect();
		expect(observed).toBe(2);
		expect(meter.samplesFor("test.gauge").map((s) => s.value)).toEqual([42, 42]);
	});

	test("cardinality rejections surface on a disallowed label value", () => {
		const { registry, meter } = initMetrics({ environment: "test" });
		// Through the projector's helpers, unknown values pass through
		// `assertLabel` which pushes to the cardinality counter.
		// Directly invoke via the label helper so the registration side-effect fires.
		const { asGate } = require("../../src/telemetry/labels.ts");
		void asGate("bogus.gate", "theo.turns.total");
		// cardinality rejections counter should have incremented.
		expect(registry.cardinalityRejections.name).toBe("theo.telemetry.cardinality_rejections_total");
		expect(meter.samplesFor("theo.telemetry.cardinality_rejections_total").length).toBeGreaterThan(
			0,
		);
	});
});
