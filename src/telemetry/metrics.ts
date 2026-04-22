/**
 * MetricsRegistry — the single place every metric instrument is declared.
 *
 * The shape is SDK-agnostic. The default implementation is an in-process
 * registry that records counters/histograms/gauges into plain maps; this
 * is what tests assert against and what runs in local dev. When an OTLP
 * exporter is wired in (follow-up after the Bun + OTel compat spike), the
 * registry gets swapped for one that forwards to `@opentelemetry/api`
 * meters — no business-code change.
 *
 * Labels are attached at emission time via `assertLabel` (see `labels.ts`).
 * Keys that fail the closed-set check are replaced with `"unknown"` and
 * counted via `cardinality_rejections`.
 */

import type {
	ObservableResult,
	Counter as OtelCounter,
	Histogram as OtelHistogram,
	Meter as OtelMeter,
	ObservableGauge as OtelObservableGauge,
} from "@opentelemetry/api";
import { registerCardinalityRejectSink } from "./labels.ts";
import type { Environment } from "./resource.ts";
import { getActiveExemplar } from "./tracer.ts";

/**
 * OTel attribute names disallow `.` in some exporters' strict mode and the
 * Prometheus remote-write path replaces dots with underscores anyway. We do
 * not depend on that normalization — labels flow through as-is and the
 * remote-write exporter handles the rewrite.
 */
function toAttributes(labels: Labels): Record<string, string | number> {
	// Labels are already `Readonly<Record<string, string | number>>`; the OTel
	// Attributes type is structurally compatible. Copy to a plain object so
	// the exporter doesn't freeze a shared reference.
	const out: Record<string, string | number> = {};
	for (const [k, v] of Object.entries(labels)) out[k] = v;
	return out;
}

// ---------------------------------------------------------------------------
// Instrument types
// ---------------------------------------------------------------------------

export type Labels = Readonly<Record<string, string | number>>;

export interface Counter {
	add(delta: number, labels?: Labels): void;
	readonly name: string;
}

export interface Histogram {
	record(value: number, labels?: Labels): void;
	readonly name: string;
}

export interface ObservableGauge {
	/** Register a callback invoked at collection time. */
	addCallback(cb: () => Promise<void> | void): void;
	/** Observe a value — normally called from inside an addCallback. */
	observe(value: number, labels?: Labels): void;
	readonly name: string;
}

// ---------------------------------------------------------------------------
// Registry shape
// ---------------------------------------------------------------------------

export interface MetricsRegistry {
	// Chat / turns
	readonly turnCounter: Counter;
	readonly turnDuration: Histogram;
	readonly inputTokens: Counter;
	readonly outputTokens: Counter;
	readonly costCounter: Counter;

	// Memory / retrieval
	readonly retrievalDuration: Histogram;
	readonly cacheHitRate: ObservableGauge;
	readonly nodesGauge: ObservableGauge;
	readonly embeddingBytesGauge: ObservableGauge;

	// Bus
	readonly eventsAppended: Counter;
	readonly handlerDuration: Histogram;
	readonly handlerErrors: Counter;
	readonly handlerLag: ObservableGauge;

	// DB
	readonly dbQueryDuration: Histogram;
	readonly dbPoolInUse: ObservableGauge;

	// Scheduler
	readonly schedulerTickDuration: Histogram;
	readonly schedulerJobsDue: ObservableGauge;

	// Process
	readonly processMemoryRss: ObservableGauge;
	readonly processEventLoopLag: ObservableGauge;

	// Goals (Phase 12a)
	readonly goalsActive: ObservableGauge;
	readonly goalsQuarantined: ObservableGauge;
	readonly taskTurns: Counter;
	readonly leaseContention: Counter;

	// Reflex (Phase 13b)
	readonly reflexReceived: Counter;
	readonly reflexRejected: Counter;
	readonly reflexRateLimited: Counter;
	readonly reflexDispatched: Counter;

	// Ideation (Phase 13b)
	readonly ideationRuns: Counter;
	readonly ideationCost: Counter;
	readonly ideationProposals: Counter;

	// Proposals (Phase 13b)
	readonly proposalsPending: ObservableGauge;
	readonly proposalsApproved: Counter;
	readonly proposalsExpired: Counter;

	// Cloud egress (Phase 13b)
	readonly cloudEgressCost: Counter;
	readonly cloudEgressTokens: Counter;

	// Degradation (Phase 13b)
	readonly degradationLevel: ObservableGauge;
	readonly autonomyViolations: Counter;

	// Advisor (Phase 14)
	readonly advisorIterations: Counter;
	readonly advisorCost: Counter;

	// Telemetry self-observability (Phase 15)
	readonly exporterDropped: Counter;
	readonly exporterQueueSaturation: ObservableGauge;
	readonly cardinalityRejections: Counter;

	// Synthetic (Phase 15)
	readonly probeDuration: Histogram;
	readonly probeFailures: Counter;

	// SLO (Phase 15)
	readonly sloErrorBudgetRemaining: ObservableGauge;
	readonly sloBurnRate: ObservableGauge;
}

// ---------------------------------------------------------------------------
// Default (in-memory) implementation
// ---------------------------------------------------------------------------

/** One recorded sample — enough for tests to assert behavior. */
export interface Sample {
	readonly value: number;
	readonly labels: Labels;
	readonly at: number;
	/** Populated on histogram samples when recorded inside an active span. */
	readonly exemplar?: { readonly traceId: string; readonly spanId: string };
}

/**
 * Per-instrument cap on the in-memory sample ring buffer. Keeps total
 * memory bounded over long runs; OTLP export and on-demand snapshots
 * operate on the retained tail.
 */
const SAMPLE_CAP = 2048;

function pushCapped(buf: Sample[], sample: Sample): void {
	buf.push(sample);
	if (buf.length > SAMPLE_CAP) buf.shift();
}

// ---------------------------------------------------------------------------
// Instrument implementations
//
// Each instrument keeps an append-only sample buffer (capped at SAMPLE_CAP)
// so tests can assert behavior without touching the SDK. When `bindOtlp` is
// called, each instrument receives an SDK counterpart and writes land in both
// places: the sample buffer (local/tests) and the OTel SDK (exported to the
// OTLP collector → Prometheus).
// ---------------------------------------------------------------------------

class LocalCounter implements Counter {
	private sdk: OtelCounter | null = null;
	constructor(
		readonly name: string,
		private readonly buf: Sample[],
	) {}
	add(delta: number, labels: Labels = {}): void {
		pushCapped(this.buf, { value: delta, labels, at: Date.now() });
		this.sdk?.add(delta, toAttributes(labels));
	}
	attachSdk(counter: OtelCounter): void {
		this.sdk = counter;
	}
}

class LocalHistogram implements Histogram {
	private sdk: OtelHistogram | null = null;
	constructor(
		readonly name: string,
		private readonly buf: Sample[],
	) {}
	record(value: number, labels: Labels = {}): void {
		const exemplar = getActiveExemplar();
		const sample: Sample =
			exemplar !== null
				? {
						value,
						labels,
						at: Date.now(),
						exemplar: { traceId: exemplar.traceId, spanId: exemplar.spanId },
					}
				: { value, labels, at: Date.now() };
		pushCapped(this.buf, sample);
		this.sdk?.record(value, toAttributes(labels));
	}
	attachSdk(histogram: OtelHistogram): void {
		this.sdk = histogram;
	}
}

class LocalObservableGauge implements ObservableGauge {
	/**
	 * The OTel SDK invokes its callback once per collection cycle and expects
	 * the user callback to call `result.observe(v, attrs)`. Our users call
	 * `gauge.observe()` directly — so during an SDK-driven collection we stash
	 * the active `ObservableResult` here, and `observe()` fans out to both the
	 * local sample buffer and the SDK result.
	 */
	private activeSdkResult: ObservableResult | null = null;
	private readonly callbacks: Array<() => Promise<void> | void> = [];

	constructor(
		readonly name: string,
		private readonly buf: Sample[],
	) {}

	addCallback(cb: () => Promise<void> | void): void {
		this.callbacks.push(cb);
	}

	observe(value: number, labels: Labels = {}): void {
		pushCapped(this.buf, { value, labels, at: Date.now() });
		this.activeSdkResult?.observe(value, toAttributes(labels));
	}

	attachSdk(gauge: OtelObservableGauge): void {
		gauge.addCallback(async (result: ObservableResult) => {
			// Stash the SDK result for the duration of the user callbacks so
			// `observe()` can dual-write. Cleared in `finally` so callbacks that
			// fire outside collection cycles never write to a stale result.
			this.activeSdkResult = result;
			try {
				for (const cb of this.callbacks) await cb();
			} finally {
				this.activeSdkResult = null;
			}
		});
	}

	/** Invoke every registered callback once — used by tests and `meter.collect()`. */
	async collect(): Promise<void> {
		for (const cb of this.callbacks) await cb();
	}
}

/**
 * The default meter keeps counters and histograms as append-only sample
 * lists, capped per instrument to `SAMPLE_CAP` so long-running processes
 * don't leak memory. When `bindOtlp(meter)` is called post-construction,
 * every existing instrument gets an SDK counterpart and subsequent writes
 * flow through both paths. Instruments created *after* bind also attach on
 * creation, so wiring order is flexible.
 */
export class InMemoryMeter {
	private readonly samples = new Map<string, Sample[]>();
	private readonly counters = new Map<string, LocalCounter>();
	private readonly histograms = new Map<string, LocalHistogram>();
	private readonly gauges = new Map<string, LocalObservableGauge>();
	private sdkMeter: OtelMeter | null = null;

	private bucket(name: string): Sample[] {
		let buf = this.samples.get(name);
		if (buf === undefined) {
			buf = [];
			this.samples.set(name, buf);
		}
		return buf;
	}

	counter(name: string): Counter {
		let c = this.counters.get(name);
		if (c === undefined) {
			c = new LocalCounter(name, this.bucket(name));
			if (this.sdkMeter !== null) c.attachSdk(this.sdkMeter.createCounter(name));
			this.counters.set(name, c);
		}
		return c;
	}

	histogram(name: string): Histogram {
		let h = this.histograms.get(name);
		if (h === undefined) {
			h = new LocalHistogram(name, this.bucket(name));
			if (this.sdkMeter !== null) h.attachSdk(this.sdkMeter.createHistogram(name));
			this.histograms.set(name, h);
		}
		return h;
	}

	observableGauge(name: string): ObservableGauge {
		let g = this.gauges.get(name);
		if (g === undefined) {
			g = new LocalObservableGauge(name, this.bucket(name));
			if (this.sdkMeter !== null) g.attachSdk(this.sdkMeter.createObservableGauge(name));
			this.gauges.set(name, g);
		}
		return g;
	}

	/**
	 * Attach an OTel SDK meter so every instrument forwards writes to the SDK
	 * in addition to the local sample buffer. Idempotent-ish: calling twice
	 * with different meters would double-report — initTelemetry calls this
	 * exactly once after `initOtlpExporters` succeeds.
	 */
	bindOtlp(sdkMeter: OtelMeter): void {
		this.sdkMeter = sdkMeter;
		for (const [name, c] of this.counters) c.attachSdk(sdkMeter.createCounter(name));
		for (const [name, h] of this.histograms) h.attachSdk(sdkMeter.createHistogram(name));
		for (const [name, g] of this.gauges) g.attachSdk(sdkMeter.createObservableGauge(name));
	}

	/** For tests: run every registered gauge callback once. */
	async collect(): Promise<void> {
		for (const g of this.gauges.values()) await g.collect();
	}

	samplesFor(name: string): readonly Sample[] {
		return this.samples.get(name) ?? [];
	}

	reset(): void {
		for (const v of this.samples.values()) v.length = 0;
	}
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

export interface MetricsConfig {
	readonly environment: Environment;
}

export interface InitializedMetrics {
	readonly registry: MetricsRegistry;
	/** Concrete in-memory meter for tests; production swaps for OTLP adapter. */
	readonly meter: InMemoryMeter;
}

/** Build the registry. Wires `assertLabel`'s rejection counter as a side effect. */
export function initMetrics(_config?: MetricsConfig): InitializedMetrics {
	const meter = new InMemoryMeter();

	const registry: MetricsRegistry = {
		turnCounter: meter.counter("theo.turns.total"),
		turnDuration: meter.histogram("theo.turns.duration_ms"),
		inputTokens: meter.counter("theo.tokens.input"),
		outputTokens: meter.counter("theo.tokens.output"),
		costCounter: meter.counter("theo.cost.usd"),

		retrievalDuration: meter.histogram("theo.retrieval.duration_ms"),
		cacheHitRate: meter.observableGauge("theo.cache.hit_rate_gauge"),
		nodesGauge: meter.observableGauge("theo.memory.nodes_gauge"),
		embeddingBytesGauge: meter.observableGauge("theo.memory.embedding_bytes_gauge"),

		eventsAppended: meter.counter("theo.bus.events_appended_total"),
		handlerDuration: meter.histogram("theo.bus.handler_duration_ms"),
		handlerErrors: meter.counter("theo.bus.handler_errors_total"),
		handlerLag: meter.observableGauge("theo.bus.handler_lag_seconds"),

		dbQueryDuration: meter.histogram("theo.db.query_duration_ms"),
		dbPoolInUse: meter.observableGauge("theo.db.pool_in_use_gauge"),

		schedulerTickDuration: meter.histogram("theo.scheduler.tick_duration_ms"),
		schedulerJobsDue: meter.observableGauge("theo.scheduler.jobs_due_gauge"),

		processMemoryRss: meter.observableGauge("theo.process.memory_rss_bytes"),
		processEventLoopLag: meter.observableGauge("theo.process.event_loop_lag_ms"),

		goalsActive: meter.observableGauge("theo.goals.active_gauge"),
		goalsQuarantined: meter.observableGauge("theo.goals.quarantined_gauge"),
		taskTurns: meter.counter("theo.goals.task_turns_total"),
		leaseContention: meter.counter("theo.goals.lease_contention_total"),

		reflexReceived: meter.counter("theo.reflex.received_total"),
		reflexRejected: meter.counter("theo.reflex.rejected_total"),
		reflexRateLimited: meter.counter("theo.reflex.rate_limited_total"),
		reflexDispatched: meter.counter("theo.reflex.dispatched_total"),

		ideationRuns: meter.counter("theo.ideation.runs_total"),
		ideationCost: meter.counter("theo.ideation.cost_usd_total"),
		ideationProposals: meter.counter("theo.ideation.proposals_total"),

		proposalsPending: meter.observableGauge("theo.proposals.pending_gauge"),
		proposalsApproved: meter.counter("theo.proposals.approved_total"),
		proposalsExpired: meter.counter("theo.proposals.expired_total"),

		cloudEgressCost: meter.counter("theo.cloud_egress.cost_usd_total"),
		cloudEgressTokens: meter.counter("theo.cloud_egress.tokens_total"),

		degradationLevel: meter.observableGauge("theo.degradation.level_gauge"),
		autonomyViolations: meter.counter("theo.autonomy.violations_total"),

		advisorIterations: meter.counter("theo.advisor.iterations_total"),
		advisorCost: meter.counter("theo.advisor.cost_usd_total"),

		exporterDropped: meter.counter("theo.telemetry.exporter_dropped_total"),
		exporterQueueSaturation: meter.observableGauge(
			"theo.telemetry.exporter_queue_saturation_gauge",
		),
		cardinalityRejections: meter.counter("theo.telemetry.cardinality_rejections_total"),

		probeDuration: meter.histogram("theo.synthetic.probe_duration_ms"),
		probeFailures: meter.counter("theo.synthetic.probe_failures_total"),

		sloErrorBudgetRemaining: meter.observableGauge("theo.slo.error_budget_remaining_ratio"),
		sloBurnRate: meter.observableGauge("theo.slo.burn_rate"),
	};

	// Wire the cardinality sink.
	registerCardinalityRejectSink((metric, label) => {
		registry.cardinalityRejections.add(1, { metric, label });
	});

	return { registry, meter };
}

/**
 * Every instrument name this registry declares, in declaration order.
 * Used by dashboards/recording-rules tests to assert only known metrics
 * are referenced.
 */
export const ALL_METRIC_NAMES: readonly string[] = [
	"theo.turns.total",
	"theo.turns.duration_ms",
	"theo.tokens.input",
	"theo.tokens.output",
	"theo.cost.usd",
	"theo.retrieval.duration_ms",
	"theo.cache.hit_rate_gauge",
	"theo.memory.nodes_gauge",
	"theo.memory.embedding_bytes_gauge",
	"theo.bus.events_appended_total",
	"theo.bus.handler_duration_ms",
	"theo.bus.handler_errors_total",
	"theo.bus.handler_lag_seconds",
	"theo.db.query_duration_ms",
	"theo.db.pool_in_use_gauge",
	"theo.scheduler.tick_duration_ms",
	"theo.scheduler.jobs_due_gauge",
	"theo.process.memory_rss_bytes",
	"theo.process.event_loop_lag_ms",
	"theo.goals.active_gauge",
	"theo.goals.quarantined_gauge",
	"theo.goals.task_turns_total",
	"theo.goals.lease_contention_total",
	"theo.reflex.received_total",
	"theo.reflex.rejected_total",
	"theo.reflex.rate_limited_total",
	"theo.reflex.dispatched_total",
	"theo.ideation.runs_total",
	"theo.ideation.cost_usd_total",
	"theo.ideation.proposals_total",
	"theo.proposals.pending_gauge",
	"theo.proposals.approved_total",
	"theo.proposals.expired_total",
	"theo.cloud_egress.cost_usd_total",
	"theo.cloud_egress.tokens_total",
	"theo.degradation.level_gauge",
	"theo.autonomy.violations_total",
	"theo.advisor.iterations_total",
	"theo.advisor.cost_usd_total",
	"theo.telemetry.exporter_dropped_total",
	"theo.telemetry.exporter_queue_saturation_gauge",
	"theo.telemetry.cardinality_rejections_total",
	"theo.synthetic.probe_duration_ms",
	"theo.synthetic.probe_failures_total",
	"theo.slo.error_budget_remaining_ratio",
	"theo.slo.burn_rate",
] as const;
