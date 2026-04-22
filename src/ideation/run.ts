/**
 * Ideation — scheduled, replay-safe, provenance-filtered.
 *
 * Ideation runs in the `ideation` priority class. Its event pattern
 * mirrors the reflex split so replay is deterministic without re-invoking
 * the LLM:
 *
 *   cron tick
 *     -> ideation.scheduled (decision: captures sampled node ids + RNG seed)
 *     -> ideation.proposed   (effect: the LLM output captured as event data)
 *     -> ideation.duplicate_suppressed | proposal.requested (decision)
 *
 * The provenance filter is the critical security control: ideation retrieval
 * reads ONLY nodes with `effective_trust IN ('owner','owner_confirmed')`.
 * Webhook-sourced content lands at `external` and is therefore invisible to
 * ideation by construction. See `foundation.md §7.8`.
 *
 * Ideation-origin proposals are hard-capped at autonomy level 2 — this is
 * enforced by `proposals/store.ts::requestProposal`.
 */

import type { Sql } from "postgres";
import { monotonicFactory } from "ulid";
import { asQueryable } from "../db/pool.ts";
import { advisorAllowed, ideationAllowed, readDegradation } from "../degradation/state.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";
import { readConsent } from "../memory/egress.ts";
import { requestProposal } from "../proposals/store.ts";

const ulid = monotonicFactory();

// ---------------------------------------------------------------------------
// Budget config
// ---------------------------------------------------------------------------

export interface IdeationBudget {
	readonly maxRunsPerWeek: number;
	readonly maxBudgetUsdPerRun: number;
	readonly maxBudgetUsdPerMonth: number;
	readonly dedupWindowDays: number;
	readonly rejectionBackoffMultiplier: number;
}

export const DEFAULT_BUDGET: IdeationBudget = {
	maxRunsPerWeek: 3,
	maxBudgetUsdPerRun: 0.5,
	maxBudgetUsdPerMonth: 10.0,
	dedupWindowDays: 30,
	rejectionBackoffMultiplier: 2.0,
};

// ---------------------------------------------------------------------------
// Candidate sampling (deterministic by seed)
// ---------------------------------------------------------------------------

export interface Candidate {
	readonly nodeId: number;
	readonly kind: string;
	readonly body: string;
}

/**
 * Provenance-filtered candidate sampling (`foundation.md §7.8`). Excludes:
 *   - effective_trust NOT IN ('owner','owner_confirmed')
 *   - kind = 'goal'   (anti-recursion)
 *   - metadata->>'origin' = 'ideation'  (anti-recursion)
 *
 * Orders by novelty (stale nodes first), capped to `limit`.
 */
export async function sampleCandidates(sql: Sql, limit: number): Promise<readonly Candidate[]> {
	const q = asQueryable(sql);
	const rows = await q<{ id: number; kind: string; body: string }[]>`
		SELECT n.id, n.kind, n.body
		FROM node n
		WHERE n.embedding IS NOT NULL
		  AND n.trust IN ('owner','owner_confirmed')
		  AND n.importance > 0.3
		  AND n.access_count > 0
		  AND n.kind != 'goal'
		  AND (n.metadata->>'origin' IS NULL OR n.metadata->>'origin' != 'ideation')
		ORDER BY n.last_accessed_at ASC NULLS FIRST, n.importance DESC
		LIMIT ${limit}
	`;
	return rows.map((r) => ({ nodeId: r.id, kind: r.kind, body: r.body }));
}

// ---------------------------------------------------------------------------
// Budget accounting
// ---------------------------------------------------------------------------

export interface BudgetCheck {
	readonly ok: boolean;
	readonly scope?: "run" | "week" | "month";
	readonly capUsd?: number;
	readonly spentUsd?: number;
}

/**
 * Read cumulative ideation spend across the past week and month. A run is
 * blocked when either cap is exceeded (or would be by the next run's cap).
 */
export async function checkBudget(
	sql: Sql,
	at: Date,
	budget: IdeationBudget,
): Promise<BudgetCheck> {
	const q = asQueryable(sql);
	const weekAgo = new Date(at.getTime() - 7 * 24 * 60 * 60_000);
	const monthAgo = new Date(at.getTime() - 30 * 24 * 60 * 60_000);

	const weekRows = await q<{ runs: number }[]>`
		SELECT COUNT(*)::int AS runs
		FROM ideation_run
		WHERE started_at >= ${weekAgo}
	`;
	const weekRuns = weekRows[0]?.runs ?? 0;
	if (weekRuns >= budget.maxRunsPerWeek) {
		return { ok: false, scope: "week", capUsd: budget.maxRunsPerWeek, spentUsd: weekRuns };
	}

	const monthRows = await q<{ spent: string | null }[]>`
		SELECT COALESCE(SUM(cost_usd), 0)::text AS spent
		FROM ideation_run
		WHERE started_at >= ${monthAgo}
	`;
	const spent = Number(monthRows[0]?.spent ?? "0");
	if (spent >= budget.maxBudgetUsdPerMonth) {
		return { ok: false, scope: "month", capUsd: budget.maxBudgetUsdPerMonth, spentUsd: spent };
	}

	return { ok: true };
}

// ---------------------------------------------------------------------------
// Proposal hashing / dedup
// ---------------------------------------------------------------------------

function normalize(text: string): string {
	return text.trim().replace(/\s+/g, " ").toLowerCase();
}

async function sha256Hex(text: string): Promise<string> {
	const buf = new TextEncoder().encode(normalize(text));
	const digest = await crypto.subtle.digest("SHA-256", buf);
	const view = new Uint8Array(digest);
	let out = "";
	for (const byte of view) {
		out += byte.toString(16).padStart(2, "0");
	}
	return out;
}

export async function hashProposal(text: string): Promise<string> {
	return sha256Hex(text);
}

// ---------------------------------------------------------------------------
// Runner contract
// ---------------------------------------------------------------------------

export interface IdeationRunner {
	run(input: IdeationRunInput): Promise<IdeationRunResult>;
}

export interface IdeationRunInput {
	readonly runId: string;
	readonly candidates: readonly Candidate[];
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly budgetCapUsd: number;
}

export interface IdeationRunResult {
	readonly proposalText: string;
	readonly referencedNodeIds: readonly number[];
	readonly confidence: number;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly costUsd: number;
	readonly iterations: readonly {
		readonly kind: "executor" | "advisor_message";
		readonly model: string;
		readonly inputTokens: number;
		readonly outputTokens: number;
		readonly costUsd: number;
	}[];
}

// ---------------------------------------------------------------------------
// Orchestrator
// ---------------------------------------------------------------------------

export interface IdeationDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly runner: IdeationRunner;
	readonly budget?: IdeationBudget;
	readonly candidateCount?: number;
	readonly model?: string;
	readonly advisorModel?: string;
	readonly now?: () => Date;
}

/**
 * Run one ideation job end-to-end. Emits `ideation.scheduled` (decision) with
 * the sampled node ids + RNG seed, runs the effect via `runner.run`, emits
 * `ideation.proposed` (or `ideation.budget_exceeded`), dedups by hash, and
 * promotes to `proposal.requested` when unique.
 */
export async function runIdeationJob(deps: IdeationDeps): Promise<void> {
	const { sql, bus, runner } = deps;
	const now = deps.now ?? ((): Date => new Date());
	const budget = deps.budget ?? DEFAULT_BUDGET;
	const candidateCount = deps.candidateCount ?? 32;
	const model = deps.model ?? "claude-sonnet-4-6";

	// Consent: ideation is autonomous and cloud-bound — block without consent.
	const consent = await readConsent(sql);
	if (!consent.autonomousCloudEgressEnabled) {
		// Suppress silently — the owner controls this via `/consent`.
		return;
	}

	// Degradation gate: level 2+ disables ideation entirely.
	const deg = await readDegradation(sql);
	if (!ideationAllowed(deg.level)) return;

	const advisorModel = advisorAllowed(deg.level, "ideation")
		? (deps.advisorModel ?? "claude-opus-4-6")
		: undefined;

	// Budget gate.
	const budgetCheck = await checkBudget(sql, now(), budget);
	const runId = ulid();
	if (!budgetCheck.ok) {
		await bus.emit({
			type: "ideation.budget_exceeded",
			version: 1,
			actor: "scheduler",
			data: {
				runId,
				scope: budgetCheck.scope ?? "run",
				capUsd: budgetCheck.capUsd ?? 0,
				spentUsd: budgetCheck.spentUsd ?? 0,
			},
			metadata: {},
		});
		return;
	}

	// 1. Deterministic sampling — record the sample in the decision event.
	const candidates = await sampleCandidates(sql, candidateCount);
	const rngSeed = runId; // ULID acts as seed — recorded in the event.
	const scheduled = await bus.emit({
		type: "ideation.scheduled",
		version: 1,
		actor: "scheduler",
		data: {
			runId,
			kgCheckpoint: null,
			sourceNodeIds: candidates.map((c) => c.nodeId),
			model,
			...(advisorModel !== undefined ? { advisorModel } : {}),
			budgetCapUsd: budget.maxBudgetUsdPerRun,
			rngSeed,
		},
		metadata: {},
	});

	// 2. Effect — run the LLM.
	const q = asQueryable(sql);
	await q`
		INSERT INTO ideation_run (run_id, started_at, status)
		VALUES (${runId}, now(), 'running')
	`;
	const runResult = await runner.run({
		runId,
		candidates,
		model,
		...(advisorModel !== undefined ? { advisorModel } : {}),
		budgetCapUsd: budget.maxBudgetUsdPerRun,
	});

	const proposalHash = await hashProposal(runResult.proposalText);

	// 3. Record the proposed event (effect's durable side).
	const proposed = await bus.emit({
		type: "ideation.proposed",
		version: 1,
		actor: "theo",
		data: {
			runId,
			proposalText: runResult.proposalText,
			proposalHash,
			referencedNodeIds: runResult.referencedNodeIds,
			confidence: runResult.confidence,
			model,
			...(advisorModel !== undefined ? { advisorModel } : {}),
			iterations: runResult.iterations,
			costUsd: runResult.costUsd,
		},
		metadata: { causeId: scheduled.id },
	});

	await q`
		UPDATE ideation_run
		SET completed_at = now(), cost_usd = ${runResult.costUsd}, status = 'completed'
		WHERE run_id = ${runId}
	`;

	// 4. Dedup — match hash against the last `dedupWindowDays` proposed events.
	// Use `<= now` (inclusive) so a concurrent ideation run emitting at the
	// same instant is still caught by the dedup.
	const currentNow = now();
	const sinceDate = new Date(currentNow.getTime() - budget.dedupWindowDays * 24 * 60 * 60_000);
	const dupRows = await q<{ data: Record<string, unknown> }[]>`
		SELECT data FROM events
		WHERE type = 'ideation.proposed'
		  AND timestamp >= ${sinceDate}
		  AND timestamp <= ${currentNow}
		  AND data->>'proposalHash' = ${proposalHash}
		  AND data->>'runId' != ${runId}
		ORDER BY timestamp DESC
		LIMIT 1
	`;
	const prior = dupRows[0];
	if (prior) {
		await bus.emit({
			type: "ideation.duplicate_suppressed",
			version: 1,
			actor: "system",
			data: {
				runId,
				proposalHash,
				originalRunId: String(prior.data["runId"]),
			},
			metadata: { causeId: proposed.id },
		});
		return;
	}

	// 5. Promote to proposal.requested.
	await requestProposal(
		{ sql, bus },
		{
			origin: "ideation",
			sourceCauseId: proposed.id,
			title: runResult.proposalText.slice(0, 80),
			summary: runResult.proposalText.slice(0, 500),
			kind: "new_goal",
			payload: { proposalText: runResult.proposalText, confidence: runResult.confidence },
			effectiveTrust: "inferred",
			autonomyDomain: "ideation.proposal",
			requiredLevel: 2, // capped by requestProposal as well
		},
	);
}

// ---------------------------------------------------------------------------
// Backoff decision handler
// ---------------------------------------------------------------------------

/**
 * Register the rejection-backoff decision handler. Three consecutive
 * `proposal.rejected` events for ideation-origin proposals double the
 * `ideation_backoff.current_interval_sec` and emit `ideation.backoff_extended`.
 */
export function registerIdeationBackoff(deps: { readonly sql: Sql; readonly bus: EventBus }): void {
	const { sql, bus } = deps;
	bus.on(
		"proposal.rejected",
		async (event: EventOfType<"proposal.rejected">) => {
			const q = asQueryable(sql);
			const rows = await q<{ origin: string }[]>`
				SELECT origin FROM proposal WHERE id = ${event.data.proposalId}
			`;
			const row = rows[0];
			if (!row || row.origin !== "ideation") return;

			const backoffRows = await q<Record<string, unknown>[]>`
				SELECT consecutive_rejections, current_interval_sec
				FROM ideation_backoff WHERE id = 'singleton'
			`;
			const backoff = backoffRows[0];
			if (!backoff) return;

			const consecutiveRejections = backoff["consecutive_rejections"] as number;
			const currentIntervalSec = backoff["current_interval_sec"] as number;
			const nextConsecutive = consecutiveRejections + 1;
			if (nextConsecutive < 3) {
				await q`
					UPDATE ideation_backoff
					SET consecutive_rejections = ${nextConsecutive}
					WHERE id = 'singleton'
				`;
				return;
			}

			const newInterval = Math.round(
				currentIntervalSec * DEFAULT_BUDGET.rejectionBackoffMultiplier,
			);
			const nextRunAt = new Date(Date.now() + newInterval * 1000);
			await q`
				UPDATE ideation_backoff
				SET consecutive_rejections = 0,
				    current_interval_sec = ${newInterval},
				    next_run_at = ${nextRunAt}
				WHERE id = 'singleton'
			`;
			await bus.emit({
				type: "ideation.backoff_extended",
				version: 1,
				actor: "system",
				data: { nextRunAt: nextRunAt.toISOString(), reason: "consecutive_rejections" },
				metadata: { causeId: event.id },
			});
		},
		{ id: "ideation-backoff", mode: "decision" },
	);
}
