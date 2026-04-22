/**
 * Telemetry module entry point.
 *
 * `initTelemetry(config, bus, sql)` builds a `TelemetryBundle` that the
 * engine hands downstream. Only `withSpan` and `shutdown` leak out of the
 * module — business code never touches logger, metrics, or tracer
 * directly.
 *
 * The module is self-contained: a biome `noRestrictedImports` rule refuses
 * any import of `src/telemetry/*` outside the module (except `engine.ts`,
 * which wires everything up).
 */

import type { Sql } from "postgres";
import type { EventBus } from "../events/bus.ts";
import {
	initOtlpExporters,
	isOtlpEnabled,
	loadOtlpConfig,
	type OtlpExporterBundle,
} from "./exporters.ts";
import { registerGauges } from "./gauges.ts";
import { type LoggerConfig, TheoLogger } from "./logger.ts";
import { type InitializedMetrics, initMetrics } from "./metrics.ts";
import { loadProfilingConfig, type ProfilingHandle, startProfiling } from "./profiling.ts";
import { TelemetryProjector } from "./projector.ts";
import { buildResource, type Environment, type ResourceConfig } from "./resource.ts";
import { type FinishedSpan, initTracer, type TracerBundle, type WithSpan } from "./tracer.ts";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface TelemetryConfig {
	readonly environment: Environment;
	readonly logger?: LoggerConfig;
	readonly resource?: Omit<ResourceConfig, "environment">;
}

// ---------------------------------------------------------------------------
// Bundle
// ---------------------------------------------------------------------------

/**
 * The public surface. `withSpan` is the only helper business code sees —
 * everything else is the telemetry module's private concern.
 */
export interface TelemetryBundle {
	readonly withSpan: WithSpan;
	readonly shutdown: () => Promise<void>;
	/**
	 * Exposed for the engine (which wires the projector to the bus) and for
	 * tests. Business code must never touch these directly.
	 */
	readonly internals: {
		readonly projector: TelemetryProjector;
		readonly metrics: InitializedMetrics;
		readonly logger: TheoLogger;
		readonly tracer: TracerBundle;
	};
}

// ---------------------------------------------------------------------------
// initTelemetry
// ---------------------------------------------------------------------------

export async function initTelemetry(
	config: TelemetryConfig,
	bus: EventBus,
	sql: Sql,
): Promise<TelemetryBundle> {
	const resource = await buildResource({
		environment: config.environment,
		...(config.resource ?? {}),
	});

	const metrics = initMetrics({ environment: config.environment });

	const logger = new TheoLogger({
		...(config.logger ?? {}),
		// Wire the logger's trace-id enrichment to the tracer after the
		// tracer is built — done below.
	});

	// OTLP exporter bootstrap (conditional). When env configures the OTLP
	// endpoint, the exporter bundle is built once; the tracer's `exporter`
	// callback forwards finished spans into it. When absent, spans land in
	// the in-memory list only.
	let otlp: OtlpExporterBundle | null = null;
	if (isOtlpEnabled()) {
		try {
			otlp = initOtlpExporters(loadOtlpConfig(), metrics);
			// Prime the OTel API cache so the span-bridge has a hot handle
			// before the first `withSpan` fires.
			await primeOtelApiCache();
		} catch (error) {
			// OTLP bootstrap must never block startup. Log and continue with
			// the in-memory tracer.
			logger.error("OTLP exporter init failed — continuing with in-memory tracer", {
				error: error instanceof Error ? error.message : String(error),
			});
			otlp = null;
		}
	}

	const traceExporterSink =
		otlp !== null
			? (span: FinishedSpan): void => forwardSpanToOtlp(span, otlp as OtlpExporterBundle)
			: undefined;

	const tracer = initTracer({
		resource,
		metrics,
		...(traceExporterSink !== undefined ? { exporter: traceExporterSink } : {}),
	});

	// Re-bind logger to the tracer's active-context getter so every log line
	// carries the current trace/span ids.
	(
		logger as unknown as { activeContext?: () => { traceId: string; spanId: string } | null }
	).activeContext = (): { traceId: string; spanId: string } | null => {
		const a = tracer.active();
		return a ? { traceId: a.traceId, spanId: a.spanId } : null;
	};

	const projector = new TelemetryProjector({ metrics: metrics.registry, logger });

	// The projector is wired to every durable event type via a single
	// subscription — the bus's `on()` dispatches strictly by type, so we
	// register once per event-type family by iterating EVENT_TYPES below.
	for (const type of EVENT_TYPES) {
		bus.on(type, projector.handleEvent as never);
	}

	registerGauges({ metrics: metrics.registry, sql });

	// Continuous profiling (Pyroscope shim). Disabled by default because the
	// upstream SDK's native `@datadog/pprof` crashes Bun; see `profiling.ts`.
	const profilingConfig = loadProfilingConfig(process.env, {
		version: resource["service.version"] ?? "unknown",
		instance: resource["service.instance.id"] ?? "unknown",
	});
	const profiling: ProfilingHandle = startProfiling(profilingConfig, logger);

	return {
		withSpan: tracer.withSpan,
		shutdown: async () => {
			await tracer.shutdown();
			await profiling.stop();
			if (otlp !== null) {
				await otlp.shutdown();
			}
		},
		internals: { projector, metrics, logger, tracer },
	};
}

// ---------------------------------------------------------------------------
// Bridge: forward in-memory `FinishedSpan` to the OTLP exporter.
//
// The in-memory tracer is SDK-agnostic (see `tracer.ts`); bridging is a thin
// adapter that constructs a `ReadableSpan` stub the BatchSpanProcessor
// tolerates. This keeps the business-code surface (`withSpan`) unchanged
// while letting OTLP see our spans.
// ---------------------------------------------------------------------------

let otelApiCache: typeof import("@opentelemetry/api") | null = null;

function forwardSpanToOtlp(span: FinishedSpan, _bundle: OtlpExporterBundle): void {
	// The SDK's `ReadableSpan` interface is wide (30+ fields, many from the
	// SDK-internal `Tracer`). We don't own a Tracer — the forward path is
	// "best effort": we create spans through the SDK tracer itself so the
	// BSP sees SDK-native objects.
	// Implementation: start and end an SDK span with matching attributes.
	// This double-instruments but keeps traces flowing end-to-end to the
	// collector without us hand-rolling a ReadableSpan.
	const api = otelApiCache;
	if (api === null) {
		// First call primes the cache — we kick off the dynamic import once
		// and let subsequent spans fall through until the module resolves.
		void primeOtelApiCache();
		return;
	}
	try {
		const sdkTracer = api.trace.getTracer("theo.bridge");
		const sdkSpan = sdkTracer.startSpan(span.name, {
			startTime: spanStartTimeMs(span),
			attributes: flatAttributes(span.attributes),
		});
		if (span.status === "error") {
			sdkSpan.recordException(
				span.errorMessage !== undefined ? new Error(span.errorMessage) : new Error("error"),
			);
			// `SpanStatusCode.ERROR` is numeric 2 in OTel API.
			sdkSpan.setStatus({ code: 2 });
		}
		sdkSpan.end(spanEndTimeMs(span));
	} catch {
		// Never propagate exporter failures — the in-memory list still works.
	}
}

async function primeOtelApiCache(): Promise<void> {
	if (otelApiCache !== null) return;
	try {
		otelApiCache = await import("@opentelemetry/api");
	} catch {
		// Leave the cache null; subsequent spans stay in-memory only.
	}
}

function spanStartTimeMs(span: FinishedSpan): number {
	// `performance.now()` returns fractional ms since an arbitrary epoch.
	// The OTel SDK accepts `[seconds, nanoseconds]` or `number` (ms). We use
	// milliseconds for simplicity; tests use the in-memory exporter.
	return Date.now() - (performance.now() - span.startMs);
}

function spanEndTimeMs(span: FinishedSpan): number {
	return Date.now() - (performance.now() - span.endMs);
}

function flatAttributes(attrs: Record<string, unknown>): Record<string, string | number | boolean> {
	const out: Record<string, string | number | boolean> = {};
	for (const [k, v] of Object.entries(attrs)) {
		if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
			out[k] = v;
		} else if (v !== null && v !== undefined) {
			out[k] = String(v);
		}
	}
	return out;
}

// ---------------------------------------------------------------------------
// Event-type enumeration — the projector subscribes to each
// ---------------------------------------------------------------------------

/**
 * Every durable event type in the domain. Declared explicitly here rather
 * than derived from `Event["type"]` so a missing case is a compile error
 * in `projector.ts` AND a missing subscription here shows up at runtime.
 */
const EVENT_TYPES = [
	"message.received",
	"turn.started",
	"turn.completed",
	"turn.failed",
	"session.created",
	"session.released",
	"session.compacting",
	"session.compacted",
	"memory.node.created",
	"memory.node.updated",
	"memory.edge.created",
	"memory.edge.expired",
	"memory.episode.created",
	"memory.core.updated",
	"memory.contradiction.detected",
	"contradiction.requested",
	"contradiction.classified",
	"episode.summarize_requested",
	"episode.summarized",
	"memory.user_model.updated",
	"memory.self_model.updated",
	"memory.skill.created",
	"memory.skill.promoted",
	"memory.node.decayed",
	"memory.pattern.synthesized",
	"memory.node.merged",
	"memory.node.importance.propagated",
	"memory.node.confidence_adjusted",
	"memory.node.accessed",
	"job.created",
	"job.triggered",
	"job.completed",
	"job.failed",
	"job.cancelled",
	"notification.created",
	"system.started",
	"system.stopped",
	"system.rollback",
	"system.degradation.healed",
	"self_update.blocked",
	"synthetic.probe.completed",
	"system.handler.dead_lettered",
	"hook.failed",
	"goal.created",
	"goal.confirmed",
	"goal.priority_changed",
	"goal.plan_updated",
	"goal.lease_acquired",
	"goal.lease_released",
	"goal.task_started",
	"goal.task_progress",
	"goal.task_yielded",
	"goal.task_completed",
	"goal.task_failed",
	"goal.task_abandoned",
	"goal.blocked",
	"goal.unblocked",
	"goal.reconsidered",
	"goal.paused",
	"goal.resumed",
	"goal.cancelled",
	"goal.completed",
	"goal.quarantined",
	"goal.redacted",
	"goal.expired",
	"webhook.received",
	"webhook.verified",
	"webhook.rejected",
	"webhook.rate_limited",
	"webhook.secret_rotated",
	"webhook.secret_grace_expired",
	"reflex.triggered",
	"reflex.thought",
	"reflex.suppressed",
	"ideation.scheduled",
	"ideation.proposed",
	"ideation.duplicate_suppressed",
	"ideation.budget_exceeded",
	"ideation.backoff_extended",
	"proposal.requested",
	"proposal.drafted",
	"proposal.approved",
	"proposal.rejected",
	"proposal.executed",
	"proposal.expired",
	"proposal.redacted",
	"policy.autonomous_cloud_egress.enabled",
	"policy.autonomous_cloud_egress.disabled",
	"policy.egress_sensitivity.updated",
	"cloud_egress.turn",
	"degradation.level_changed",
] as const;

export type { WithSpan } from "./tracer.ts";
