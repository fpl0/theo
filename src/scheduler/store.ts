/**
 * JobStore: scheduled_job + job_execution CRUD.
 *
 * Intentionally thin. All schedule math (nextRunAt advancement, cron parsing)
 * lives in `cron.ts`; the store just persists the values it is handed. Job
 * IDs and execution IDs are minted by callers via `newJobId` / `newExecutionId`
 * so tests can thread deterministic IDs through.
 *
 * Numeric columns (`max_budget_usd`, `cost_usd`) are declared as numeric so
 * PostgreSQL returns them as strings — we coerce to `number` at the boundary.
 * The typed row helpers absorb this so the rest of the codebase sees plain
 * numbers.
 */

import type { Sql } from "postgres";
import type {
	ExecutionId,
	ExecutionUpdate,
	JobExecution,
	JobId,
	ScheduledJob,
	ScheduledJobInput,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Row shapes from PostgreSQL
//
// Rows come back as plain objects with snake_case keys (the column names).
// Using `Record<string, unknown>` with explicit property access keeps the
// biome naming-convention rule — which forbids snake_case interface members —
// quiet without suppressing anything.
// ---------------------------------------------------------------------------

type Row = Record<string, unknown>;

function mapJob(row: Row): ScheduledJob {
	return {
		id: row["id"] as JobId,
		name: row["name"] as string,
		cron: row["cron"] as string | null,
		agent: row["agent"] as string,
		prompt: row["prompt"] as string,
		enabled: row["enabled"] as boolean,
		maxDurationMs: row["max_duration_ms"] as number,
		// numeric comes back as a string from postgres.js; parse defensively.
		maxBudgetUsd: Number(row["max_budget_usd"]),
		lastRunAt: row["last_run_at"] as Date | null,
		nextRunAt: row["next_run_at"] as Date,
		createdAt: row["created_at"] as Date,
		updatedAt: row["updated_at"] as Date,
	};
}

function mapExecution(row: Row): JobExecution {
	const costRaw = row["cost_usd"] as string | null;
	return {
		id: row["id"] as ExecutionId,
		jobId: row["job_id"] as JobId,
		status: row["status"] as "running" | "completed" | "failed",
		startedAt: row["started_at"] as Date,
		completedAt: row["completed_at"] as Date | null,
		durationMs: row["duration_ms"] as number | null,
		tokensUsed: row["tokens_used"] as number | null,
		costUsd: costRaw === null ? null : Number(costRaw),
		errorMessage: row["error_message"] as string | null,
		resultSummary: row["result_summary"] as string | null,
	};
}

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

export interface JobStore {
	/** Insert a new scheduled job. Fails if the name is already taken. */
	create(input: ScheduledJobInput): Promise<ScheduledJob>;

	/** Fetch by ULID id. Returns null if not found. */
	getById(id: JobId): Promise<ScheduledJob | null>;

	/** Fetch by name (unique). Returns null if not found. */
	getByName(name: string): Promise<ScheduledJob | null>;

	/** List all jobs, enabled or not, ordered by creation. */
	list(): Promise<readonly ScheduledJob[]>;

	/**
	 * Return enabled jobs with `next_run_at <= now`. Used by the tick loop to
	 * find what to fire this tick, including boundary (exactly-now) matches.
	 */
	getDueJobs(now: Date): Promise<readonly ScheduledJob[]>;

	/**
	 * Return enabled jobs with `next_run_at < now` — strictly past. Used once
	 * at startup so jobs that missed their window while Theo was offline fire
	 * exactly once (not once per missed tick).
	 */
	getOverdueJobs(now: Date): Promise<readonly ScheduledJob[]>;

	/** Disable a job (enabled = false). Idempotent. */
	disable(id: JobId): Promise<void>;

	/** Advance next_run_at and stamp last_run_at = now(). Used after each run. */
	updateNextRun(id: JobId, nextRunAt: Date, lastRunAt: Date): Promise<void>;

	/** Insert a new `running` execution row. Returns the row as loaded. */
	createExecution(jobId: JobId, executionId: ExecutionId): Promise<JobExecution>;

	/** Transition an execution to `completed` or `failed`. */
	completeExecution(id: ExecutionId, update: ExecutionUpdate): Promise<void>;

	/** Fetch a single execution by id (mostly for tests). */
	getExecution(id: ExecutionId): Promise<JobExecution | null>;

	/** List executions for a job, newest first. */
	listExecutions(jobId: JobId, limit: number): Promise<readonly JobExecution[]>;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export function createJobStore(sql: Sql): JobStore {
	async function create(input: ScheduledJobInput): Promise<ScheduledJob> {
		const rows = await sql`
			INSERT INTO scheduled_job (
				id, name, cron, agent, prompt, enabled,
				max_duration_ms, max_budget_usd, next_run_at
			) VALUES (
				${input.id}, ${input.name}, ${input.cron}, ${input.agent},
				${input.prompt}, ${input.enabled},
				${input.maxDurationMs}, ${input.maxBudgetUsd}, ${input.nextRunAt}
			)
			RETURNING *
		`;
		const row = rows[0];
		if (row === undefined) {
			throw new Error(`Failed to create scheduled_job "${input.name}"`);
		}
		return mapJob(row);
	}

	async function getById(id: JobId): Promise<ScheduledJob | null> {
		const rows = await sql`SELECT * FROM scheduled_job WHERE id = ${id}`;
		const row = rows[0];
		return row === undefined ? null : mapJob(row);
	}

	async function getByName(name: string): Promise<ScheduledJob | null> {
		const rows = await sql`SELECT * FROM scheduled_job WHERE name = ${name}`;
		const row = rows[0];
		return row === undefined ? null : mapJob(row);
	}

	async function list(): Promise<readonly ScheduledJob[]> {
		const rows = await sql`SELECT * FROM scheduled_job ORDER BY created_at ASC, id ASC`;
		return rows.map(mapJob);
	}

	async function getDueJobs(now: Date): Promise<readonly ScheduledJob[]> {
		// ORDER BY next_run_at so earlier-due jobs go first within a tick.
		const rows = await sql`
			SELECT * FROM scheduled_job
			WHERE enabled = true
			  AND next_run_at <= ${now}
			ORDER BY next_run_at ASC, id ASC
		`;
		return rows.map(mapJob);
	}

	async function getOverdueJobs(now: Date): Promise<readonly ScheduledJob[]> {
		const rows = await sql`
			SELECT * FROM scheduled_job
			WHERE enabled = true
			  AND next_run_at < ${now}
			ORDER BY next_run_at ASC, id ASC
		`;
		return rows.map(mapJob);
	}

	async function disable(id: JobId): Promise<void> {
		await sql`UPDATE scheduled_job SET enabled = false WHERE id = ${id}`;
	}

	async function updateNextRun(id: JobId, nextRunAt: Date, lastRunAt: Date): Promise<void> {
		await sql`
			UPDATE scheduled_job
			SET next_run_at = ${nextRunAt}, last_run_at = ${lastRunAt}
			WHERE id = ${id}
		`;
	}

	async function createExecution(jobId: JobId, executionId: ExecutionId): Promise<JobExecution> {
		const rows = await sql`
			INSERT INTO job_execution (id, job_id, status)
			VALUES (${executionId}, ${jobId}, 'running')
			RETURNING *
		`;
		const row = rows[0];
		if (row === undefined) {
			throw new Error(`Failed to create job_execution ${executionId}`);
		}
		return mapExecution(row);
	}

	async function completeExecution(id: ExecutionId, update: ExecutionUpdate): Promise<void> {
		if (update.status === "completed") {
			await sql`
				UPDATE job_execution
				SET status = 'completed',
				    completed_at = now(),
				    duration_ms = ${update.durationMs},
				    result_summary = ${update.resultSummary},
				    tokens_used = ${update.tokensUsed},
				    cost_usd = ${update.costUsd}
				WHERE id = ${id}
			`;
			return;
		}
		await sql`
			UPDATE job_execution
			SET status = 'failed',
			    completed_at = now(),
			    duration_ms = ${update.durationMs},
			    error_message = ${update.errorMessage}
			WHERE id = ${id}
		`;
	}

	async function getExecution(id: ExecutionId): Promise<JobExecution | null> {
		const rows = await sql`SELECT * FROM job_execution WHERE id = ${id}`;
		const row = rows[0];
		return row === undefined ? null : mapExecution(row);
	}

	async function listExecutions(jobId: JobId, limit: number): Promise<readonly JobExecution[]> {
		const rows = await sql`
			SELECT * FROM job_execution
			WHERE job_id = ${jobId}
			ORDER BY started_at DESC
			LIMIT ${limit}
		`;
		return rows.map(mapExecution);
	}

	return {
		create,
		getById,
		getByName,
		list,
		getDueJobs,
		getOverdueJobs,
		disable,
		updateNextRun,
		createExecution,
		completeExecution,
		getExecution,
		listExecutions,
	};
}
