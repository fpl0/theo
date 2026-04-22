/**
 * OTLP exporter bootstrap.
 *
 * When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, Theo exports spans and metrics
 * over OTLP/HTTP to the configured collector. When unset, nothing is exported
 * and the in-process tracer/meter are the sole signal sinks (still useful for
 * tests and dev).
 *
 * Exporter queues are bounded per the plan's "observability never degrades
 * the agent" principle — a collector outage drops spans/metrics, counts the
 * drops, and does NOT block the main loop.
 *
 * Bun + OTel compatibility (see plan §Bun + OTel JS SDK compatibility): the
 * raw `@opentelemetry/sdk-trace-base` + `@opentelemetry/sdk-metrics` packages
 * load cleanly; `@opentelemetry/sdk-node` is NOT used because its Node-only
 * instrumentations (fs, http, dns auto-instrumentation) assume a Node runtime
 * surface Bun doesn't fully provide. `@pyroscope/nodejs` crashes Bun (native
 * module via `@datadog/pprof` — see `profiling.ts`).
 */

import { metrics, trace } from "@opentelemetry/api";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import {
	BasicTracerProvider,
	BatchSpanProcessor,
	type ReadableSpan,
	type SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import type { InitializedMetrics } from "./metrics.ts";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface OtlpExporterConfig {
	/** Base endpoint — e.g., `http://localhost:4318`. Paths for traces/metrics
	 *  are derived by appending `/v1/traces` and `/v1/metrics`. */
	readonly endpoint: string;
	/** Max spans buffered in the BatchSpanProcessor queue. */
	readonly maxQueueSize: number;
	/** Max spans per export call. */
	readonly maxExportBatchSize: number;
	/** Delay between exports, ms. */
	readonly scheduledDelayMillis: number;
	/** Per-export network timeout, ms. */
	readonly exportTimeoutMillis: number;
	/** Interval between metric exports, ms. */
	readonly metricExportIntervalMillis: number;
}

export const DEFAULT_OTLP_CONFIG: OtlpExporterConfig = {
	endpoint: "http://localhost:4318",
	maxQueueSize: 2048,
	maxExportBatchSize: 512,
	scheduledDelayMillis: 5_000,
	exportTimeoutMillis: 10_000,
	metricExportIntervalMillis: 10_000,
};

/** The SDK-backed exporter bundle. `shutdown()` flushes and terminates both. */
export interface OtlpExporterBundle {
	readonly tracerProvider: BasicTracerProvider;
	readonly meterProvider: MeterProvider;
	readonly shutdown: () => Promise<void>;
	/** Drain one span into the exporter queue. Counts drops when saturated. */
	readonly exportSpan: (span: ReadableSpan) => void;
}

// ---------------------------------------------------------------------------
// Counting wrapper around BatchSpanProcessor
// ---------------------------------------------------------------------------

interface QueueAwareProcessor extends SpanProcessor {
	readonly _maxQueueSize?: number;
	readonly _finishedSpans?: readonly ReadableSpan[];
}

/**
 * Wraps a `BatchSpanProcessor` to count spans dropped due to queue saturation.
 *
 * The SDK's BSP does not expose public drop metrics; we approximate by
 * inspecting the internal queue length (documented private) before every
 * `onEnd`. Two consecutive Bun-safe reads agree on OTel 1.30.x so the probe
 * is stable for now; if the SDK renames the field, the counter stalls but
 * no spans are dropped silently — the inner processor still sees every span.
 */
class CountingSpanProcessor implements SpanProcessor {
	constructor(
		private readonly inner: BatchSpanProcessor,
		private readonly onDrop: (reason: "queue_full") => void,
		private readonly onEndCb?: (span: ReadableSpan) => void,
	) {}

	onStart(): void {
		// No-op — BatchSpanProcessor handles span lifecycle internally.
	}

	onEnd(span: ReadableSpan): void {
		const q = this.inner as unknown as QueueAwareProcessor;
		const queueLen = (q._finishedSpans?.length ?? 0) as number;
		const maxSize = (q._maxQueueSize ?? 0) as number;
		if (maxSize > 0 && queueLen >= maxSize) {
			this.onDrop("queue_full");
			// Do not forward — inner queue is full.
			return;
		}
		this.inner.onEnd(span);
		this.onEndCb?.(span);
	}

	forceFlush(): Promise<void> {
		return this.inner.forceFlush();
	}

	shutdown(): Promise<void> {
		return this.inner.shutdown();
	}
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

/**
 * Build and install an OTLP-backed tracer + meter provider. The global OTel
 * API pointers are updated so `trace.getTracer(...)` and
 * `metrics.getMeter(...)` return the OTLP-backed implementations.
 *
 * `theoMetrics` is the in-process registry; the returned bundle forwards
 * drops to `theoMetrics.registry.exporterDropped` so dashboards observe
 * pipeline health.
 */
export function initOtlpExporters(
	config: OtlpExporterConfig,
	theoMetrics: InitializedMetrics,
): OtlpExporterBundle {
	const traceUrl = `${config.endpoint.replace(/\/$/u, "")}/v1/traces`;
	const metricUrl = `${config.endpoint.replace(/\/$/u, "")}/v1/metrics`;

	const spanExporter = new OTLPTraceExporter({ url: traceUrl });
	const batchProcessor = new BatchSpanProcessor(spanExporter, {
		maxQueueSize: config.maxQueueSize,
		maxExportBatchSize: config.maxExportBatchSize,
		scheduledDelayMillis: config.scheduledDelayMillis,
		exportTimeoutMillis: config.exportTimeoutMillis,
	});
	const countingProcessor = new CountingSpanProcessor(batchProcessor, () => {
		theoMetrics.registry.exporterDropped.add(1, { signal: "span" });
	});

	const tracerProvider = new BasicTracerProvider({
		spanProcessors: [countingProcessor],
	});
	trace.setGlobalTracerProvider(tracerProvider);

	const metricExporter = new OTLPMetricExporter({ url: metricUrl });
	const metricReader = new PeriodicExportingMetricReader({
		exporter: metricExporter,
		exportIntervalMillis: config.metricExportIntervalMillis,
		exportTimeoutMillis: config.exportTimeoutMillis,
	});
	const meterProvider = new MeterProvider({ readers: [metricReader] });
	metrics.setGlobalMeterProvider(meterProvider);

	return {
		tracerProvider,
		meterProvider,
		shutdown: async (): Promise<void> => {
			// Sequential to avoid two parallel graceful shutdowns racing on the
			// same http connection pool.
			try {
				await tracerProvider.shutdown();
			} catch {
				// Shutdown must not throw — swallow and keep going.
			}
			try {
				await meterProvider.shutdown();
			} catch {
				// Shutdown must not throw.
			}
		},
		exportSpan: (span: ReadableSpan): void => {
			countingProcessor.onEnd(span);
		},
	};
}

/** True when the env is configured for OTLP export. */
export function isOtlpEnabled(env: Record<string, string | undefined> = process.env): boolean {
	return (
		typeof env["OTEL_EXPORTER_OTLP_ENDPOINT"] === "string" &&
		env["OTEL_EXPORTER_OTLP_ENDPOINT"].length > 0
	);
}

/** Read OTLP config from env, falling back to defaults. */
export function loadOtlpConfig(
	env: Record<string, string | undefined> = process.env,
): OtlpExporterConfig {
	return {
		...DEFAULT_OTLP_CONFIG,
		endpoint: env["OTEL_EXPORTER_OTLP_ENDPOINT"] ?? DEFAULT_OTLP_CONFIG.endpoint,
	};
}
