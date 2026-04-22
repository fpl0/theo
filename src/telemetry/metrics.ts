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

import { registerCardinalityRejectSink } from "./labels.ts";
import type { Environment } from "./resource.ts";
import { getActiveExemplar } from "./tracer.ts";

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
	readonly redactions: Counter;
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

/**
 * The default meter keeps counters and histograms as append-only sample
 * lists, capped per instrument to `SAMPLE_CAP` so long-running processes
 * don't leak memory. An OTLP adapter would instead forward to an SDK meter.
 */
export class InMemoryMeter {
	private readonly samples = new Map<string, Sample[]>();
	private readonly callbacks = new Map<string, Array<() => Promise<void> | void>>();

	private bucket(name: string): Sample[] {
		let buf = this.samples.get(name);
		if (buf === undefined) {
			buf = [];
			this.samples.set(name, buf);
		}
		return buf;
	}

	counter(name: string): Counter {
		const buf = this.bucket(name);
		return {
			name,
			add: (delta, labels = {}): void => {
				pushCapped(buf, { value: delta, labels, at: Date.now() });
			},
		};
	}

	histogram(name: string): Histogram {
		const buf = this.bucket(name);
		return {
			name,
			record: (value, labels = {}): void => {
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
				pushCapped(buf, sample);
			},
		};
	}

	observableGauge(name: string): ObservableGauge {
		const buf = this.bucket(name);
		this.callbacks.set(name, this.callbacks.get(name) ?? []);
		return {
			name,
			addCallback: (cb): void => {
				(this.callbacks.get(name) ?? []).push(cb);
			},
			observe: (value, labels = {}): void => {
				pushCapped(buf, { value, labels, at: Date.now() });
			},
		};
	}

	/** For tests: run every registered gauge callback once. */
	async collect(): Promise<void> {
		for (const cbs of this.callbacks.values()) {
			for (const cb of cbs) await cb();
		}
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
		redactions: meter.counter("theo.telemetry.redactions_total"),
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
	"theo.telemetry.redactions_total",
	"theo.telemetry.cardinality_rejections_total",
	"theo.synthetic.probe_duration_ms",
	"theo.synthetic.probe_failures_total",
	"theo.slo.error_budget_remaining_ratio",
	"theo.slo.burn_rate",
] as const;
