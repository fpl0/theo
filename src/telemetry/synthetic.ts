/**
 * Synthetic canary prober.
 *
 * Issues a periodic "ping" turn via the `internal.scheduler` gate to detect
 * alive-but-stuck failures — the class where `launchd` sees a live process
 * but Theo cannot actually produce a turn. The prober emits a dedicated
 * `synthetic.probe.completed` event so probe results are durable,
 * replayable, and filtered out of cost dashboards.
 *
 * The synthetic gate tag (`internal.scheduler`) means probe turns count
 * against the scheduler's budget; they do not pollute cost-per-user
 * rollups.
 */

import type { TurnResult } from "../chat/types.ts";
import { describeError } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import { newUlid } from "../events/ids.ts";
import { unrefTimer } from "../util/timers.ts";
import { asProbeFailReason } from "./labels.ts";
import type { InitializedMetrics } from "./metrics.ts";

export interface ChatHandleLike {
	handleMessage(body: string, gate: string): Promise<TurnResult>;
}

export interface ProbeDeps {
	readonly chat: ChatHandleLike;
	readonly bus: EventBus;
	readonly metrics: InitializedMetrics;
	/** Probe timeout budget, ms. Default: 30s. */
	readonly timeoutMs?: number;
	/** Probe body. Kept minimal so probe cost stays bounded. */
	readonly body?: string;
}

export const DEFAULT_PROBE_TIMEOUT_MS = 30_000;
export const DEFAULT_PROBE_BODY = "ping";

/**
 * Run one probe turn. Always resolves — probe failures are values, never
 * thrown. Emits `synthetic.probe.completed` with the outcome so the owner
 * sees probe health in the event log.
 */
export async function runProbe(deps: ProbeDeps): Promise<void> {
	const probeId = newUlid();
	const timeoutMs = deps.timeoutMs ?? DEFAULT_PROBE_TIMEOUT_MS;
	const body = deps.body ?? DEFAULT_PROBE_BODY;
	const start = performance.now();

	let ok = false;
	let reason: string | undefined;
	try {
		const result = await withTimeout(
			deps.chat.handleMessage(body, "internal.scheduler"),
			timeoutMs,
		);
		if (result.ok) {
			ok = true;
		} else {
			ok = false;
			reason = result.error;
		}
	} catch (err) {
		ok = false;
		reason = classifyProbeError(err);
	}

	const durationMs = performance.now() - start;
	if (ok) {
		deps.metrics.registry.probeDuration.record(durationMs);
	} else {
		const bucket = asProbeFailReason(reason ?? "exception", "theo.synthetic.probe_failures_total");
		deps.metrics.registry.probeFailures.add(1, { reason: bucket });
	}

	await deps.bus.emit({
		type: "synthetic.probe.completed",
		version: 1,
		actor: "scheduler",
		data: {
			probeId,
			ok,
			durationMs,
			...(reason !== undefined ? { reason } : {}),
		},
		metadata: {},
	});
}

/**
 * Convert an arbitrary probe error into the closed-set bucket used by
 * `theo.synthetic.probe_failures_total{reason}`.
 */
export function classifyProbeError(err: unknown): "timeout" | "exception" {
	if (/timeout|timed out/iu.test(describeError(err))) return "timeout";
	return "exception";
}

/** Race a promise against a timeout. Rejects with "timeout" when exceeded. */
async function withTimeout<T>(inner: Promise<T>, timeoutMs: number): Promise<T> {
	let timer: ReturnType<typeof setTimeout> | undefined;
	const timeoutPromise = new Promise<never>((_, reject) => {
		timer = setTimeout(() => reject(new Error("probe timeout")), timeoutMs);
	});
	try {
		return await Promise.race([inner, timeoutPromise]);
	} finally {
		if (timer !== undefined) clearTimeout(timer);
	}
}

// ---------------------------------------------------------------------------
// Periodic scheduler (in-process)
// ---------------------------------------------------------------------------

export interface ProbeSchedulerConfig {
	/** Probe cadence, ms. Default: 5 minutes. */
	readonly intervalMs: number;
	/** Timeout budget per probe, ms. */
	readonly timeoutMs: number;
	/** Probe body. */
	readonly body: string;
}

export const DEFAULT_PROBE_SCHEDULER_CONFIG: ProbeSchedulerConfig = {
	intervalMs: 5 * 60_000,
	timeoutMs: DEFAULT_PROBE_TIMEOUT_MS,
	body: DEFAULT_PROBE_BODY,
};

/**
 * Runs `runProbe` on a fixed cadence. `start()` kicks off the timer; `stop()`
 * awaits the current probe (if any) and clears the interval. Safe to call
 * `start()` more than once — the second call is a no-op.
 */
export class SyntheticProbeScheduler {
	private readonly deps: ProbeDeps;
	private readonly config: ProbeSchedulerConfig;
	private timer: ReturnType<typeof setInterval> | null = null;
	private inFlight: Promise<void> | null = null;

	constructor(deps: ProbeDeps, config: Partial<ProbeSchedulerConfig> = {}) {
		this.deps = deps;
		this.config = { ...DEFAULT_PROBE_SCHEDULER_CONFIG, ...config };
	}

	start(): void {
		if (this.timer !== null) return;
		this.timer = setInterval(() => {
			if (this.inFlight !== null) return; // coalesce: skip if previous still running
			const deps: ProbeDeps = {
				chat: this.deps.chat,
				bus: this.deps.bus,
				metrics: this.deps.metrics,
				timeoutMs: this.config.timeoutMs,
				body: this.config.body,
			};
			const p = runProbe(deps).finally(() => {
				this.inFlight = null;
			});
			this.inFlight = p;
			// Surface errors to the console but never throw from the timer.
			p.catch((err: unknown) => {
				console.error("synthetic probe unexpected error", describeError(err));
			});
		}, this.config.intervalMs);
		unrefTimer(this.timer);
	}

	async stop(): Promise<void> {
		if (this.timer !== null) {
			clearInterval(this.timer);
			this.timer = null;
		}
		if (this.inFlight !== null) {
			try {
				await this.inFlight;
			} catch {
				// runProbe never throws; defensive.
			}
		}
	}
}
