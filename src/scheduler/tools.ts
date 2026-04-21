/**
 * MCP scheduler tools.
 *
 * Exposes three tools Theo can call from inside a turn: schedule a job,
 * list current jobs, and cancel (disable) a job. The tools are shallow
 * wrappers over `JobStore` + `cron.ts` so the LLM never has to know about
 * ULID minting or cron math.
 *
 * Mounted under a dedicated `scheduler` MCP server so the allowlist can
 * be gated separately from memory tools (`mcp__scheduler__*`).
 */

import {
	createSdkMcpServer,
	type McpSdkServerConfigWithInstance,
	tool,
} from "@anthropic-ai/claude-agent-sdk";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import type { EventBus } from "../events/bus.ts";
import { isValidCron, nextRun } from "./cron.ts";
import type { JobStore } from "./store.ts";
import { type JobId, newJobId } from "./types.ts";

export interface SchedulerToolDependencies {
	readonly store: JobStore;
	/** Clock injection so tests can freeze time. */
	readonly now?: () => Date;
	/**
	 * Optional event bus — when provided, `schedule_job` / `cancel_job` emit
	 * `job.created` / `job.cancelled` audit events. Tests that only care
	 * about store state can omit it.
	 */
	readonly bus?: EventBus;
}

function errorResult(error: unknown): CallToolResult {
	const message = error instanceof Error ? error.message : String(error);
	return {
		content: [{ type: "text", text: `Error: ${message}` }],
		isError: true,
	};
}

export function scheduleJobTool(deps: SchedulerToolDependencies) {
	return tool(
		"schedule_job",
		"Schedule a recurring or one-off job. Cron jobs fire on the cron schedule; " +
			"one-off jobs (cron=null) fire once at the supplied time. Prefer scheduling " +
			"over polling — the scheduler handles retries, overdue recovery, and cost caps.",
		{
			name: z.string().min(1).max(64),
			cron: z.string().nullable(),
			agent: z.string().default("main"),
			prompt: z.string().min(1).max(4000),
			maxDurationMs: z.number().int().positive().default(300_000),
			maxBudgetUsd: z.number().positive().max(5).default(0.1),
			runAt: z.string().datetime().optional(),
		},
		async (args) => {
			try {
				const now = deps.now?.() ?? new Date();
				let computedNext: Date;
				if (args.cron !== null) {
					if (!isValidCron(args.cron)) {
						return errorResult(new Error(`Invalid cron expression: ${args.cron}`));
					}
					computedNext = nextRun(args.cron, now);
				} else {
					if (args.runAt === undefined) {
						return errorResult(new Error("One-off jobs (cron=null) require runAt (ISO-8601)."));
					}
					const parsed = new Date(args.runAt);
					if (Number.isNaN(parsed.getTime())) {
						return errorResult(new Error(`Invalid runAt timestamp: ${args.runAt}`));
					}
					computedNext = parsed;
				}

				const job = await deps.store.create({
					id: newJobId(),
					name: args.name,
					cron: args.cron,
					agent: args.agent,
					prompt: args.prompt,
					enabled: true,
					maxDurationMs: args.maxDurationMs,
					maxBudgetUsd: args.maxBudgetUsd,
					nextRunAt: computedNext,
				});
				if (deps.bus !== undefined) {
					await deps.bus.emit({
						type: "job.created",
						version: 1,
						actor: "scheduler",
						data: { jobId: job.id, name: job.name, cron: job.cron },
						metadata: {},
					});
				}
				return {
					content: [
						{
							type: "text",
							text: `Scheduled "${job.name}" (${job.id}), next run ${job.nextRunAt.toISOString()}`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function listJobsTool(deps: SchedulerToolDependencies) {
	return tool(
		"list_jobs",
		"List all scheduled jobs — enabled or not — with their cron, agent, and next run.",
		{
			enabledOnly: z.boolean().default(false),
		},
		async ({ enabledOnly }) => {
			try {
				const jobs = await deps.store.list();
				const filtered = enabledOnly ? jobs.filter((j) => j.enabled) : jobs;
				if (filtered.length === 0) {
					return { content: [{ type: "text", text: "No scheduled jobs." }] };
				}
				const lines = filtered.map(
					(j) =>
						`- ${j.name} [${j.enabled ? "enabled" : "disabled"}] ` +
						`cron="${j.cron ?? "(one-off)"}" agent=${j.agent} ` +
						`next=${j.nextRunAt.toISOString()}`,
				);
				return {
					content: [{ type: "text", text: lines.join("\n") }],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function cancelJobTool(deps: SchedulerToolDependencies) {
	return tool(
		"cancel_job",
		"Cancel (disable) a scheduled job by name. The job row stays in the database " +
			"for audit; the scheduler simply stops considering it.",
		{
			name: z.string().min(1),
		},
		async ({ name }) => {
			try {
				const job = await deps.store.getByName(name);
				if (job === null) {
					return errorResult(new Error(`No job named "${name}"`));
				}
				await deps.store.disable(job.id as JobId);
				if (deps.bus !== undefined) {
					await deps.bus.emit({
						type: "job.cancelled",
						version: 1,
						actor: "scheduler",
						data: { jobId: job.id, jobName: job.name },
						metadata: {},
					});
				}
				return {
					content: [{ type: "text", text: `Cancelled "${name}" (${job.id})` }],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function schedulerToolList(deps: SchedulerToolDependencies) {
	return [scheduleJobTool(deps), listJobsTool(deps), cancelJobTool(deps)];
}

/**
 * Create the scheduler MCP server. Tool names register as
 * `mcp__scheduler__*` — callers must allowlist that prefix.
 */
export function createSchedulerServer(
	deps: SchedulerToolDependencies,
): McpSdkServerConfigWithInstance {
	return createSdkMcpServer({
		name: "scheduler",
		tools: schedulerToolList(deps),
	});
}
