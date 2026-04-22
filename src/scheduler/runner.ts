/**
 * Scheduler: tick loop, job execution, overdue handling.
 *
 * Single-runner design for Phase 12 — `maxConcurrent = 1` by default. Phase
 * 12a and 13b extend this file with priority classes and preemption; the
 * `AbortController` timeout machinery below is the hook those phases will
 * reuse. For now, the runner's job is simple: every tick, pick up enabled
 * jobs whose `next_run_at` has arrived, fire them one at a time, record
 * events + executions, and advance their schedules.
 *
 * Jobs run via an injectable `queryFn` (default: the Claude Agent SDK
 * `query()`) — tests swap in a canned generator that yields SDK-shaped
 * messages without spawning the subprocess.
 */

import {
	type McpSdkServerConfigWithInstance,
	type Options,
	type Query,
	query as sdkQuery,
} from "@anthropic-ai/claude-agent-sdk";
import { advisorSettings } from "../chat/subagents.ts";
import type { EventBus } from "../events/bus.ts";
import type { TurnErrorType } from "../events/types.ts";
import { nextRun } from "./cron.ts";
import type { JobStore } from "./store.ts";
import {
	type JobId,
	type JobResult,
	newExecutionId,
	newJobId,
	type ScheduledJob,
	type SchedulerConfig,
	type SubagentDefinition,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default tick interval: once a minute, matching standard cron granularity. */
const DEFAULT_TICK_INTERVAL_MS = 60_000;

/** Default concurrency cap: one job at a time. 12a/13b extend this. */
const DEFAULT_MAX_CONCURRENT = 1;

/** Short result summary persisted on completed executions. */
const SUMMARY_MAX_CHARS = 500;

/** Poll interval while `stop()` waits for in-flight jobs to drain. */
const STOP_POLL_INTERVAL_MS = 50;

import { unrefTimer } from "../util/timers.ts";

class JobTurnError extends Error {
	constructor(
		message: string,
		readonly errorType: TurnErrorType,
	) {
		super(message);
	}
}

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export type QueryFn = (params: { prompt: string; options?: Options }) => Query;

export interface SchedulerDependencies {
	readonly store: JobStore;
	readonly bus: EventBus;
	readonly memoryServer: McpSdkServerConfigWithInstance;
	readonly subagents: Readonly<Record<string, SubagentDefinition>>;
	readonly builtins?: ReadonlyArray<import("./types.ts").BuiltinJobSeed>;
	readonly config?: Partial<SchedulerConfig>;
	/** Test seam. Real SDK `query()` is used when omitted. */
	readonly queryFn?: QueryFn;
	/** Optional clock. Defaults to `Date.now()`. */
	readonly now?: () => Date;
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

export class Scheduler {
	private readonly store: JobStore;
	private readonly bus: EventBus;
	private readonly memoryServer: McpSdkServerConfigWithInstance;
	private readonly subagents: Readonly<Record<string, SubagentDefinition>>;
	private readonly builtins: ReadonlyArray<import("./types.ts").BuiltinJobSeed>;
	private readonly tickIntervalMs: number;
	private readonly maxConcurrent: number;
	private readonly queryFn: QueryFn;
	private readonly now: () => Date;

	private intervalId: ReturnType<typeof setInterval> | null = null;
	/**
	 * In-flight job IDs. Guards against a single job being fired twice when
	 * a tick overlaps with the previous tick's execution (e.g., a long-running
	 * job whose next_run_at has already re-qualified it).
	 */
	private readonly activeJobs = new Set<JobId>();

	constructor(deps: SchedulerDependencies) {
		this.store = deps.store;
		this.bus = deps.bus;
		this.memoryServer = deps.memoryServer;
		this.subagents = deps.subagents;
		this.builtins = deps.builtins ?? [];
		this.tickIntervalMs = deps.config?.tickIntervalMs ?? DEFAULT_TICK_INTERVAL_MS;
		this.maxConcurrent = deps.config?.maxConcurrent ?? DEFAULT_MAX_CONCURRENT;
		this.queryFn = deps.queryFn ?? sdkQuery;
		this.now = deps.now ?? ((): Date => new Date());
	}

	/**
	 * Seed built-in jobs (only those not already present), fire any jobs that
	 * are overdue once, and start the tick loop.
	 *
	 * Idempotent at the seeding layer — `getByName` gates insertion.
	 */
	async start(): Promise<void> {
		if (this.intervalId !== null) return;

		for (const seed of this.builtins) {
			const existing = await this.store.getByName(seed.name);
			if (existing !== null) continue;
			const job = await this.store.create({
				id: newJobId(),
				name: seed.name,
				cron: seed.cron,
				agent: seed.agent,
				prompt: seed.prompt,
				enabled: true,
				maxDurationMs: seed.maxDurationMs,
				maxBudgetUsd: seed.maxBudgetUsd,
				nextRunAt: nextRun(seed.cron, this.now()),
			});
			await this.bus.emit({
				type: "job.created",
				version: 1,
				actor: "scheduler",
				data: { jobId: job.id, name: job.name, cron: job.cron },
				metadata: {},
			});
		}

		// Fire any overdue jobs exactly once. We collect the IDs up-front so a
		// long-running overdue job doesn't re-qualify itself on the same pass.
		await this.runOverdueJobs();

		// Start the periodic tick. `setInterval` fires the first tick only
		// AFTER `tickIntervalMs` elapses, which is the contract we want —
		// `runOverdueJobs` covers the startup case.
		this.intervalId = setInterval(() => {
			void this.tick().catch((error: unknown) => {
				const message = error instanceof Error ? error.message : String(error);
				console.error(`Scheduler tick failed: ${message}`);
			});
		}, this.tickIntervalMs);
		// Don't keep the process alive just for the tick loop — the gate's
		// read loop or the scheduler's own work is the keep-alive, not the
		// timer. Without unref, `bun test` would hang on the interval.
		unrefTimer(this.intervalId);
	}

	/**
	 * Stop the tick loop, then wait for in-flight executions to drain. Does
	 * not force-abort anything — jobs get to reach their own terminal state
	 * (success, failure, or timeout).
	 */
	async stop(): Promise<void> {
		if (this.intervalId !== null) {
			clearInterval(this.intervalId);
			this.intervalId = null;
		}
		while (this.activeJobs.size > 0) {
			await new Promise<void>((resolve) => setTimeout(resolve, STOP_POLL_INTERVAL_MS));
		}
	}

	/** True while the tick loop is active. */
	isRunning(): boolean {
		return this.intervalId !== null;
	}

	/** Current in-flight execution count — mostly for tests. */
	activeCount(): number {
		return this.activeJobs.size;
	}

	/**
	 * One tick: query due jobs, fire whatever the concurrency cap allows. We
	 * intentionally `await` each execution sequentially when maxConcurrent is
	 * 1 (the default); under higher caps, jobs launch in sequence within the
	 * tick but overlap through their async lifetimes.
	 */
	async tick(): Promise<void> {
		const now = this.now();
		const dueJobs = await this.store.getDueJobs(now);

		for (const job of dueJobs) {
			if (this.activeJobs.has(job.id)) continue;
			if (this.activeJobs.size >= this.maxConcurrent) break;
			// Launch but don't await — activeJobs gates concurrency, and we
			// want the loop free to consider the next job once the gate frees.
			// When maxConcurrent=1, this effectively becomes sequential because
			// the second iteration hits the break.
			void this.executeJob(job);
		}
	}

	/**
	 * Execute a single job end-to-end: emit `job.triggered`, run the agent
	 * turn, record the execution terminal state, emit `job.completed` or
	 * `job.failed`, emit `notification.created` when warranted, and advance
	 * the schedule.
	 */
	async executeJob(job: ScheduledJob): Promise<void> {
		if (this.activeJobs.has(job.id)) return;
		this.activeJobs.add(job.id);

		const executionId = newExecutionId();

		try {
			await this.bus.emit({
				type: "job.triggered",
				version: 1,
				actor: "scheduler",
				data: { jobId: job.id, jobName: job.name, executionId },
				metadata: {},
			});

			const execution = await this.store.createExecution(job.id, executionId);
			const startedAt = execution.startedAt.getTime();

			try {
				const result = await this.runAgentTurn(job);
				const durationMs = Date.now() - startedAt;
				const summary = result.summary.slice(0, SUMMARY_MAX_CHARS);

				await this.store.completeExecution(executionId, {
					status: "completed",
					durationMs,
					resultSummary: summary,
					tokensUsed: result.tokensUsed,
					costUsd: result.costUsd,
				});

				await this.bus.emit({
					type: "job.completed",
					version: 1,
					actor: "scheduler",
					data: {
						jobId: job.id,
						jobName: job.name,
						executionId,
						durationMs,
						summary,
						tokensUsed: result.tokensUsed,
						costUsd: result.costUsd,
					},
					metadata: {},
				});

				if (result.notification !== null) {
					await this.bus.emit({
						type: "notification.created",
						version: 1,
						actor: "scheduler",
						data: { source: job.name, body: result.notification },
						metadata: {},
					});
				}
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				const errorType: TurnErrorType =
					error instanceof JobTurnError ? error.errorType : "error_during_execution";
				const durationMs = Date.now() - startedAt;

				await this.store.completeExecution(executionId, {
					status: "failed",
					durationMs,
					errorMessage: message,
				});

				await this.bus.emit({
					type: "job.failed",
					version: 1,
					actor: "scheduler",
					data: {
						jobId: job.id,
						jobName: job.name,
						executionId,
						durationMs,
						errorType,
						message,
					},
					metadata: {},
				});
			}

			// Advance the schedule regardless of outcome — a failed job still
			// needs its next_run_at bumped so it doesn't fire every tick.
			const completedAt = this.now();
			if (job.cron !== null) {
				const next = nextRun(job.cron, completedAt);
				await this.store.updateNextRun(job.id, next, completedAt);
			} else {
				await this.store.disable(job.id);
			}
		} finally {
			this.activeJobs.delete(job.id);
		}
	}

	// -------------------------------------------------------------------------
	// Internal: agent-turn execution
	// -------------------------------------------------------------------------

	/**
	 * Build a single-turn `query()` call. Errors propagate so `executeJob`
	 * can record them uniformly. Terminal SDK errors (`error_max_turns`,
	 * etc.) are translated into thrown Errors so the caller's catch handles
	 * them the same as subprocess failures.
	 */
	private async runAgentTurn(job: ScheduledJob): Promise<JobResult> {
		const subagent = this.subagents[job.agent];
		if (subagent === undefined) {
			throw new Error(`Unknown subagent "${job.agent}" for job "${job.name}"`);
		}

		const controller = new AbortController();
		const timeoutId = setTimeout(() => {
			controller.abort();
		}, job.maxDurationMs);
		unrefTimer(timeoutId);

		try {
			const systemPrompt = [
				subagent.systemPromptPrefix,
				`You are executing scheduled job "${job.name}".`,
				"Use memory tools to read and write relevant information.",
				"Be concise — summarize findings and actions taken.",
			]
				.filter((line) => line.length > 0)
				.join("\n");

			// Advisor tool per systemic decision §13 — see `advisorSettings`.
			const settings = advisorSettings(subagent.advisorModel);

			const options: Options = {
				model: subagent.model,
				systemPrompt,
				// Empty settingSources isolates the turn from CLAUDE.md / user
				// settings; the assembled prompt is the only instruction source.
				settingSources: [],
				mcpServers: { memory: this.memoryServer },
				allowedTools: ["mcp__memory__*"],
				maxTurns: subagent.maxTurns,
				maxBudgetUsd: job.maxBudgetUsd,
				// Scheduler jobs never resume an existing session — each turn
				// is a fresh conversation scoped to the job's prompt.
				persistSession: false,
				permissionMode: "bypassPermissions",
				allowDangerouslySkipPermissions: true,
				abortController: controller,
				...(settings !== undefined ? { settings } : {}),
			};

			const generator = this.queryFn({ prompt: job.prompt, options });

			let responseText = "";
			let tokensUsed: number | null = null;
			let costUsd: number | null = null;
			let failure: { subtype: TurnErrorType; errors: readonly string[] } | null = null;

			for await (const message of generator) {
				if (message.type !== "result") continue;
				if (message.subtype === "success") {
					responseText = message.result;
					tokensUsed = message.usage.output_tokens;
					costUsd = message.total_cost_usd;
				} else {
					failure = { subtype: message.subtype, errors: message.errors };
				}
			}

			if (failure !== null) {
				const reason = failure.errors.length > 0 ? failure.errors.join("; ") : failure.subtype;
				throw new JobTurnError(`${failure.subtype}: ${reason}`, failure.subtype);
			}

			const trimmed = responseText.trim();
			return {
				summary: trimmed,
				notification: trimmed.length > 0 ? trimmed : null,
				tokensUsed,
				costUsd,
			};
		} finally {
			clearTimeout(timeoutId);
		}
	}

	/**
	 * Fire every currently-overdue job exactly once, sequentially. Called on
	 * start so jobs that missed their window during downtime aren't lost.
	 */
	private async runOverdueJobs(): Promise<void> {
		const overdue = await this.store.getOverdueJobs(this.now());
		for (const job of overdue) {
			// Sequential await: overdue jobs should not fan out — the startup
			// catch-up is best-effort, not a burst.
			await this.executeJob(job);
		}
	}
}
