/**
 * Scheduler type surface.
 *
 * Jobs and executions both use ULID keys — the id is sortable, timestamp-
 * embedded, and can be minted by the caller before the INSERT fires. Branded
 * string types keep `JobId` / `ExecutionId` / `EventId` from colliding at the
 * type level even though they all ride the same ULID string shape at runtime.
 */

import { monotonicFactory } from "ulid";

const ulid = monotonicFactory();

// ---------------------------------------------------------------------------
// Branded IDs
// ---------------------------------------------------------------------------

export type JobId = string & { readonly __brand: "JobId" };
export type ExecutionId = string & { readonly __brand: "ExecutionId" };

export function newJobId(): JobId {
	return ulid() as JobId;
}

export function newExecutionId(): ExecutionId {
	return ulid() as ExecutionId;
}

// ---------------------------------------------------------------------------
// Persisted records
// ---------------------------------------------------------------------------

/**
 * A scheduled job. `cron` is null for one-off jobs; those disable themselves
 * after their single execution. `nextRunAt` is the wall-clock instant the
 * tick loop will compare against `now()` to decide whether to fire.
 */
export interface ScheduledJob {
	readonly id: JobId;
	readonly name: string;
	readonly cron: string | null;
	readonly agent: string;
	readonly prompt: string;
	readonly enabled: boolean;
	readonly maxDurationMs: number;
	readonly maxBudgetUsd: number;
	readonly lastRunAt: Date | null;
	readonly nextRunAt: Date;
	readonly createdAt: Date;
	readonly updatedAt: Date;
}

/**
 * A single run of a job. `running` rows with a completed_at in the past but
 * no terminal status row are orphaned — the scheduler does not currently
 * reconcile those, so graceful shutdown must be trusted.
 */
export interface JobExecution {
	readonly id: ExecutionId;
	readonly jobId: JobId;
	readonly status: "running" | "completed" | "failed";
	readonly startedAt: Date;
	readonly completedAt: Date | null;
	readonly durationMs: number | null;
	readonly tokensUsed: number | null;
	readonly costUsd: number | null;
	readonly errorMessage: string | null;
	readonly resultSummary: string | null;
}

// ---------------------------------------------------------------------------
// Input shapes (what callers supply)
// ---------------------------------------------------------------------------

/**
 * Input for creating a scheduled job. `id` and `nextRunAt` are computed by
 * the caller (typically via `newJobId()` + `nextRun(cron, now)`), so the
 * store owns no policy about ID generation or schedule math.
 */
export interface ScheduledJobInput {
	readonly id: JobId;
	readonly name: string;
	readonly cron: string | null;
	readonly agent: string;
	readonly prompt: string;
	readonly enabled: boolean;
	readonly maxDurationMs: number;
	readonly maxBudgetUsd: number;
	readonly nextRunAt: Date;
}

/**
 * Built-in job seed — cron is required for non-nullable scheduling and no ID
 * is supplied yet (the runner assigns it on seed).
 */
export interface BuiltinJobSeed {
	readonly name: string;
	readonly cron: string;
	readonly agent: string;
	readonly prompt: string;
	readonly maxDurationMs: number;
	readonly maxBudgetUsd: number;
}

/**
 * Update payload for `completeExecution`. Either "completed" (with cost +
 * summary) or "failed" (with errorMessage). The fields not applicable to a
 * variant stay undefined.
 */
export type ExecutionUpdate =
	| {
			readonly status: "completed";
			readonly durationMs: number;
			readonly resultSummary: string;
			readonly tokensUsed: number | null;
			readonly costUsd: number | null;
	  }
	| {
			readonly status: "failed";
			readonly durationMs: number;
			readonly errorMessage: string;
	  };

// ---------------------------------------------------------------------------
// Subagents + scheduler config
// ---------------------------------------------------------------------------

/**
 * Subagent definition surfaced to the scheduler. Phase 14 provides the
 * canonical catalog in `src/chat/subagents.ts`; this shape is the
 * scheduler's projection of it. `advisorModel` is optional — when set,
 * the runner passes it through as `options.settings.advisorModel` so the
 * SDK enables the server-side advisor tool. Reflex-speed subagents
 * (scanner, consolidator) leave it unset.
 */
export interface SubagentDefinition {
	readonly model: string;
	readonly maxTurns: number;
	readonly systemPromptPrefix: string;
	readonly advisorModel?: string;
}

export interface SchedulerConfig {
	/** Wall-clock interval between tick-loop evaluations, in ms. */
	readonly tickIntervalMs: number;
	/** Hard cap on in-flight executions. Phase 12 ships with 1; 12a/13b extend. */
	readonly maxConcurrent: number;
}

// ---------------------------------------------------------------------------
// Agent-turn result (runner → store)
// ---------------------------------------------------------------------------

/**
 * Summary of a single agent turn. `notification` is optional — the runner
 * emits a `notification.created` event only when the result is worth
 * surfacing. `summary` is what the store persists (short).
 */
export interface JobResult {
	readonly summary: string;
	readonly notification: string | null;
	readonly tokensUsed: number | null;
	readonly costUsd: number | null;
}
