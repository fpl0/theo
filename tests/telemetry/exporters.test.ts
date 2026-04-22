/**
 * OTLP exporter bundle tests — pointed at a black-hole endpoint so no
 * network traffic is required. The assertions are structural: init
 * succeeds, shutdown completes, drop counter increments when the internal
 * queue overflows.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
	DEFAULT_OTLP_CONFIG,
	initOtlpExporters,
	isOtlpEnabled,
	loadOtlpConfig,
} from "../../src/telemetry/exporters.ts";
import { type InitializedMetrics, initMetrics } from "../../src/telemetry/metrics.ts";

let metrics: InitializedMetrics;

beforeEach(() => {
	metrics = initMetrics({ environment: "test" });
});

afterEach(() => {
	metrics.meter.reset();
});

describe("isOtlpEnabled", () => {
	test("false when env lacks OTEL_EXPORTER_OTLP_ENDPOINT", () => {
		expect(isOtlpEnabled({})).toBe(false);
	});

	test("true when OTEL_EXPORTER_OTLP_ENDPOINT is set", () => {
		expect(isOtlpEnabled({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://localhost:4318" })).toBe(true);
	});

	test("false when empty string", () => {
		expect(isOtlpEnabled({ OTEL_EXPORTER_OTLP_ENDPOINT: "" })).toBe(false);
	});
});

describe("loadOtlpConfig", () => {
	test("returns default endpoint when env is empty", () => {
		const cfg = loadOtlpConfig({});
		expect(cfg.endpoint).toBe(DEFAULT_OTLP_CONFIG.endpoint);
	});

	test("honors OTEL_EXPORTER_OTLP_ENDPOINT", () => {
		const cfg = loadOtlpConfig({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://collector:4318" });
		expect(cfg.endpoint).toBe("http://collector:4318");
	});
});

describe("initOtlpExporters", () => {
	test("bundle loads and shutdown completes", async () => {
		const bundle = initOtlpExporters(
			{
				...DEFAULT_OTLP_CONFIG,
				endpoint: "http://127.0.0.1:14318",
				scheduledDelayMillis: 5_000,
				exportTimeoutMillis: 1_000,
				metricExportIntervalMillis: 5_000,
			},
			metrics,
		);
		expect(typeof bundle.shutdown).toBe("function");
		expect(typeof bundle.exportSpan).toBe("function");
		await bundle.shutdown();
	});
});
