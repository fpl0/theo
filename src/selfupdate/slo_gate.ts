/**
 * SLO pre-merge gate.
 *
 * Before `gh pr merge --auto` lands a self-update, we query Prometheus for
 * every SLO's `error_budget_remaining_ratio` and the `burn_rate` recording
 * rules over the fast-burn windows. If any SLO is in fast-burn territory
 * (budget remaining < 10% OR burn-rate over the 1h+5m windows > 14.4), the
 * merge is blocked and `self_update.blocked` is emitted.
 *
 * This file is intentionally small — the heavy lifting (aggregation,
 * ratio math) lives in Prometheus recording rules shipped under
 * `ops/observability/prometheus/recording_rules.yaml`. Here we only
 * evaluate the published series.
 */

import type { EventBus } from "../events/bus.ts";

// ---------------------------------------------------------------------------
// SLO definitions
// ---------------------------------------------------------------------------

export interface SloDefinition {
	readonly id: string;
	/** Prometheus series for error-budget remaining, 0-1. */
	readonly budgetRemainingSeries: string;
	/** Prometheus series for burn rate over the 1h window. */
	readonly burnRate1hSeries: string;
	/** Fast-burn threshold (per Google SRE workbook, 2% budget in 1h). */
	readonly fastBurnThreshold: number;
	/** Minimum budget ratio before merges are blocked. */
	readonly budgetFloor: number;
}

/**
 * Default SLO set — matches the recording rules shipped in
 * `ops/observability/prometheus/recording_rules.yaml`.
 */
export const DEFAULT_SLOS: readonly SloDefinition[] = [
	{
		id: "turn_available",
		budgetRemainingSeries: "theo:slo:error_budget_remaining_ratio",
		burnRate1hSeries: "theo:slo:turns_available:burn_rate_1h",
		fastBurnThreshold: 14.4,
		budgetFloor: 0.1,
	},
	{
		id: "turn_latency",
		budgetRemainingSeries: "theo:slo:turns_latency:error_budget_remaining_ratio",
		burnRate1hSeries: "theo:slo:turns_latency:burn_rate_1h",
		fastBurnThreshold: 14.4,
		budgetFloor: 0.1,
	},
] as const;

// ---------------------------------------------------------------------------
// Prometheus client (minimal, just enough for the gate)
// ---------------------------------------------------------------------------

export interface PromInstantResult {
	readonly metric: Record<string, string>;
	readonly value: number;
}

/**
 * Query a Prometheus instant series. Returns the first sample, or `null`
 * when the series is empty. A network error re-throws so the caller can
 * treat "couldn't read SLOs" as a safety failure.
 */
export async function promInstant(
	prometheusUrl: string,
	query: string,
	fetcher: typeof fetch = fetch,
): Promise<PromInstantResult | null> {
	const url = new URL(`${prometheusUrl.replace(/\/$/u, "")}/api/v1/query`);
	url.searchParams.set("query", query);
	const response = await fetcher(url.toString(), {
		method: "GET",
		headers: { accept: "application/json" },
	});
	if (!response.ok) {
		throw new Error(`prom query failed: ${response.status.toString()} ${response.statusText}`);
	}
	const body = (await response.json()) as {
		data?: { result?: Array<{ metric: Record<string, string>; value: [number, string] }> };
		status?: string;
	};
	if (body.status !== "success") {
		throw new Error(`prom query status: ${String(body.status)}`);
	}
	const first = body.data?.result?.[0];
	if (first === undefined) return null;
	const [, rawValue] = first.value;
	return { metric: first.metric, value: Number(rawValue) };
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

export interface SloGateDecision {
	readonly ok: boolean;
	/** Per-SLO measurements captured at decision time. */
	readonly measurements: readonly {
		readonly slo: string;
		readonly budgetRemaining: number | null;
		readonly burnRate1h: number | null;
		readonly blocked: boolean;
		readonly reason?: "budget_exhausted" | "fast_burn" | "prometheus_unreachable";
	}[];
}

export interface SloGateDeps {
	readonly prometheusUrl: string;
	readonly slos?: readonly SloDefinition[];
	readonly fetcher?: typeof fetch;
	readonly bus?: EventBus;
}

/**
 * Run the gate. When any SLO is in fast-burn OR its budget is below the
 * floor, the gate returns `ok: false`; when `deps.bus` is provided, a
 * `self_update.blocked` event is emitted for each offending SLO.
 */
export async function checkSlosBeforeMerge(deps: SloGateDeps): Promise<SloGateDecision> {
	const slos = deps.slos ?? DEFAULT_SLOS;
	const fetcher = deps.fetcher ?? fetch;
	const measurements: SloGateDecision["measurements"] = await Promise.all(
		slos.map(async (slo) => {
			let budget: number | null = null;
			let burn: number | null = null;
			try {
				const [b, r] = await Promise.all([
					promInstant(deps.prometheusUrl, slo.budgetRemainingSeries, fetcher),
					promInstant(deps.prometheusUrl, slo.burnRate1hSeries, fetcher),
				]);
				budget = b?.value ?? null;
				burn = r?.value ?? null;
			} catch {
				// Prometheus unreachable — treat as safety failure.
				return { slo: slo.id, budgetRemaining: null, burnRate1h: null, blocked: true } as const;
			}

			const budgetExhausted = budget !== null && budget < slo.budgetFloor;
			const fastBurn = burn !== null && burn > slo.fastBurnThreshold;
			if (budgetExhausted) {
				return {
					slo: slo.id,
					budgetRemaining: budget,
					burnRate1h: burn,
					blocked: true,
					reason: "budget_exhausted",
				} as const;
			}
			if (fastBurn) {
				return {
					slo: slo.id,
					budgetRemaining: budget,
					burnRate1h: burn,
					blocked: true,
					reason: "fast_burn",
				} as const;
			}
			return {
				slo: slo.id,
				budgetRemaining: budget,
				burnRate1h: burn,
				blocked: false,
			} as const;
		}),
	);

	const blockers = measurements.filter((m) => m.blocked);
	if (blockers.length > 0 && deps.bus !== undefined) {
		for (const m of blockers) {
			await deps.bus.emit({
				type: "self_update.blocked",
				version: 1,
				actor: "system",
				data: {
					slo: m.slo,
					budgetRemainingRatio: m.budgetRemaining ?? 0,
					reason: m.reason ?? "prometheus_unreachable",
				},
				metadata: {},
			});
		}
	}

	return { ok: blockers.length === 0, measurements };
}
