/**
 * Integration tests for JobStore.
 *
 * Requires Docker PostgreSQL via `just up` + `just test-db`. Tests clean the
 * scheduler tables between cases, not between files, because each `describe`
 * reuses the same pool.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Pool } from "../../src/db/pool.ts";
import { createJobStore, type JobStore } from "../../src/scheduler/store.ts";
import {
	type JobId,
	newExecutionId,
	newJobId,
	type ScheduledJobInput,
} from "../../src/scheduler/types.ts";
import { createTestPool } from "../helpers.ts";

let pool: Pool;
let store: JobStore;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) throw new Error(`Test setup failed: ${connectResult.error.message}`);
	store = createJobStore(pool.sql);
});

afterAll(async () => {
	if (pool) await pool.end();
});

beforeEach(async () => {
	// Order: executions first because they FK-reference jobs.
	await pool.sql`TRUNCATE job_execution, scheduled_job CASCADE`;
});

function jobInput(overrides: Partial<ScheduledJobInput> = {}): ScheduledJobInput {
	const defaults: ScheduledJobInput = {
		id: newJobId(),
		name: `job-${Math.random().toString(36).slice(2, 10)}`,
		cron: "0 */6 * * *",
		agent: "main",
		prompt: "do a thing",
		enabled: true,
		maxDurationMs: 60_000,
		maxBudgetUsd: 0.05,
		nextRunAt: new Date(Date.UTC(2026, 0, 1, 0, 0, 0)),
	};
	return { ...defaults, ...overrides };
}

describe("JobStore.create / getById / getByName", () => {
	test("create returns the persisted job with branded id", async () => {
		const input = jobInput({ name: "alpha" });
		const job = await store.create(input);
		expect(job.id).toBe(input.id);
		expect(job.name).toBe("alpha");
		expect(job.enabled).toBe(true);
		// numeric column round-trips as number, not string.
		expect(typeof job.maxBudgetUsd).toBe("number");
		expect(job.maxBudgetUsd).toBeCloseTo(0.05);
	});

	test("getById returns the job", async () => {
		const input = jobInput({ name: "beta" });
		await store.create(input);
		const fetched = await store.getById(input.id);
		expect(fetched).not.toBeNull();
		expect(fetched?.name).toBe("beta");
	});

	test("getById returns null for unknown ids", async () => {
		const fetched = await store.getById(newJobId());
		expect(fetched).toBeNull();
	});

	test("getByName returns the job", async () => {
		await store.create(jobInput({ name: "consolidation" }));
		const fetched = await store.getByName("consolidation");
		expect(fetched?.name).toBe("consolidation");
	});

	test("getByName returns null for unknown names", async () => {
		const fetched = await store.getByName("does-not-exist");
		expect(fetched).toBeNull();
	});

	test("duplicate names fail with a unique-violation error", async () => {
		await store.create(jobInput({ name: "dup" }));
		expect(store.create(jobInput({ name: "dup" }))).rejects.toThrow();
	});
});

describe("JobStore.getDueJobs / getOverdueJobs", () => {
	test("getDueJobs returns enabled jobs with next_run_at <= now", async () => {
		const pastDue = jobInput({
			name: "past",
			nextRunAt: new Date(Date.UTC(2026, 0, 1, 0, 0, 0)),
		});
		const future = jobInput({
			name: "future",
			nextRunAt: new Date(Date.UTC(2099, 0, 1, 0, 0, 0)),
		});
		await store.create(pastDue);
		await store.create(future);

		const due = await store.getDueJobs(new Date(Date.UTC(2026, 0, 15)));
		expect(due.length).toBe(1);
		expect(due[0]?.name).toBe("past");
	});

	test("getDueJobs excludes disabled jobs", async () => {
		const input = jobInput({ name: "disabled" });
		await store.create(input);
		await store.disable(input.id);
		const due = await store.getDueJobs(new Date(Date.UTC(2099, 0, 1)));
		expect(due.length).toBe(0);
	});

	test("getOverdueJobs returns only strictly-past jobs", async () => {
		const exact = jobInput({
			name: "exact",
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		});
		const overdue = jobInput({
			name: "overdue",
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 10, 0, 0)),
		});
		await store.create(exact);
		await store.create(overdue);

		const rows = await store.getOverdueJobs(new Date(Date.UTC(2026, 0, 15, 12, 0, 0)));
		expect(rows.map((r) => r.name)).toEqual(["overdue"]);
	});

	test("jobs ordered by next_run_at ascending", async () => {
		await store.create(
			jobInput({ name: "later", nextRunAt: new Date(Date.UTC(2026, 0, 15, 10, 0, 0)) }),
		);
		await store.create(
			jobInput({ name: "sooner", nextRunAt: new Date(Date.UTC(2026, 0, 15, 9, 0, 0)) }),
		);
		const due = await store.getDueJobs(new Date(Date.UTC(2026, 0, 16)));
		expect(due.map((j) => j.name)).toEqual(["sooner", "later"]);
	});
});

describe("JobStore.disable / updateNextRun", () => {
	test("disable flips enabled to false", async () => {
		const input = jobInput({ name: "to-cancel" });
		await store.create(input);
		await store.disable(input.id);
		const fetched = await store.getById(input.id);
		expect(fetched?.enabled).toBe(false);
	});

	test("disable is idempotent", async () => {
		const input = jobInput({ name: "double-cancel" });
		await store.create(input);
		await store.disable(input.id);
		await store.disable(input.id);
		const fetched = await store.getById(input.id);
		expect(fetched?.enabled).toBe(false);
	});

	test("updateNextRun advances both timestamps", async () => {
		const input = jobInput({ name: "advance" });
		await store.create(input);

		const nextAt = new Date(Date.UTC(2026, 5, 1, 0, 0, 0));
		const lastAt = new Date(Date.UTC(2026, 4, 15, 0, 0, 0));
		await store.updateNextRun(input.id, nextAt, lastAt);

		const fetched = await store.getById(input.id);
		expect(fetched?.nextRunAt.getTime()).toBe(nextAt.getTime());
		expect(fetched?.lastRunAt?.getTime()).toBe(lastAt.getTime());
	});
});

describe("JobStore.createExecution / completeExecution", () => {
	async function seedJob(): Promise<JobId> {
		const input = jobInput({ name: "exec-host" });
		await store.create(input);
		return input.id;
	}

	test("createExecution inserts a running row", async () => {
		const jobId = await seedJob();
		const executionId = newExecutionId();
		const exec = await store.createExecution(jobId, executionId);
		expect(exec.id).toBe(executionId);
		expect(exec.jobId).toBe(jobId);
		expect(exec.status).toBe("running");
		expect(exec.completedAt).toBeNull();
	});

	test("completeExecution(completed) sets status, duration, summary, cost", async () => {
		const jobId = await seedJob();
		const executionId = newExecutionId();
		await store.createExecution(jobId, executionId);

		await store.completeExecution(executionId, {
			status: "completed",
			durationMs: 1234,
			resultSummary: "did the thing",
			tokensUsed: 987,
			costUsd: 0.0123,
		});

		const fetched = await store.getExecution(executionId);
		expect(fetched?.status).toBe("completed");
		expect(fetched?.durationMs).toBe(1234);
		expect(fetched?.resultSummary).toBe("did the thing");
		expect(fetched?.tokensUsed).toBe(987);
		expect(fetched?.costUsd).toBeCloseTo(0.0123);
		expect(fetched?.completedAt).not.toBeNull();
	});

	test("failed terminal status records error message", async () => {
		const jobId = await seedJob();
		const executionId = newExecutionId();
		await store.createExecution(jobId, executionId);

		await store.completeExecution(executionId, {
			status: "failed",
			durationMs: 4321,
			errorMessage: "boom",
		});

		const fetched = await store.getExecution(executionId);
		expect(fetched?.status).toBe("failed");
		expect(fetched?.errorMessage).toBe("boom");
		expect(fetched?.resultSummary).toBeNull();
	});

	test("listExecutions returns executions newest first", async () => {
		const jobId = await seedJob();
		const first = newExecutionId();
		await store.createExecution(jobId, first);
		// Small delay so started_at differs.
		await new Promise((r) => setTimeout(r, 5));
		const second = newExecutionId();
		await store.createExecution(jobId, second);

		const rows = await store.listExecutions(jobId, 10);
		expect(rows.map((r) => r.id)).toEqual([second, first]);
	});

	test("deleting a job cascades to its executions", async () => {
		const jobId = await seedJob();
		await store.createExecution(jobId, newExecutionId());
		await pool.sql`DELETE FROM scheduled_job WHERE id = ${jobId}`;
		const remaining = await store.listExecutions(jobId, 10);
		expect(remaining.length).toBe(0);
	});
});
