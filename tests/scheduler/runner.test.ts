/**
 * Integration tests for Scheduler (tick loop + execution lifecycle).
 *
 * The SDK is stubbed via the `queryFn` seam — each test hands the runner a
 * canned async generator that yields SDK-shaped messages (success or error),
 * so there's no subprocess, no network, and cost/token accounting is exact.
 *
 * The database and event bus are real — the runner's contract with both is
 * load-bearing, and mocking them out would hide the bugs this phase cares
 * about most.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type {
	McpSdkServerConfigWithInstance,
	Options,
	Query,
	SDKMessage,
} from "@anthropic-ai/claude-agent-sdk";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { Scheduler } from "../../src/scheduler/runner.ts";
import { createJobStore, type JobStore } from "../../src/scheduler/store.ts";
import {
	type BuiltinJobSeed,
	newJobId,
	type SubagentDefinition,
} from "../../src/scheduler/types.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

let pool: Pool;
let bus: EventBus;
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
	await pool.sql`TRUNCATE job_execution, scheduled_job CASCADE`;
	await cleanEventTables(pool.sql);
	bus = createTestBus(pool.sql);
});

afterEach(async () => {
	await bus.flush();
	await bus.stop();
});

// ---------------------------------------------------------------------------
// SDK stubs
// ---------------------------------------------------------------------------

/**
 * Minimal MCP server placeholder — the scheduler never calls into it during
 * these tests (the stub queryFn never resolves tool calls), but the type
 * requires an instance. The double-cast through `unknown` keeps us within
 * project conventions (no `any`, no biome-ignore) while still satisfying
 * the opaque `McpServer` type.
 */
const stubMemoryServer: McpSdkServerConfigWithInstance = {
	type: "sdk",
	name: "memory",
	instance: {} as unknown as McpSdkServerConfigWithInstance["instance"],
};

interface QueryOutcome {
	readonly kind: "success";
	readonly text: string;
	readonly tokensUsed?: number;
	readonly costUsd?: number;
}

interface ErrorOutcome {
	readonly kind: "error";
	readonly subtype:
		| "error_during_execution"
		| "error_max_turns"
		| "error_max_budget_usd"
		| "error_max_structured_output_retries";
	readonly errors: readonly string[];
}

interface ThrowOutcome {
	readonly kind: "throw";
	readonly message: string;
}

interface HangOutcome {
	readonly kind: "hang";
}

type Outcome = QueryOutcome | ErrorOutcome | ThrowOutcome | HangOutcome;

/**
 * Build a stub `queryFn` that returns an async generator yielding a single
 * SDK `result` message (or throws / hangs until the abort signal fires).
 *
 * The SDK's `SDKMessage` union requires a fully-populated `usage` object
 * (`NonNullableUsage`) and a branded UUID. Rather than reproduce the full
 * shape for each test, we build minimal result payloads and hand them
 * through a single `as SDKMessage` cast — the runner only reads `type`,
 * `subtype`, `result`, `usage.output_tokens`, `total_cost_usd`, and
 * `errors`, so every other field is uninspected.
 */
function stubQueryFn(outcome: Outcome): (params: { prompt: string; options?: Options }) => Query {
	return ({ options }) => {
		const gen = (async function* (): AsyncGenerator<SDKMessage> {
			if (outcome.kind === "throw") {
				throw new Error(outcome.message);
			}
			if (outcome.kind === "hang") {
				// Wait for abort — reject when the signal fires. Keeps the
				// generator suspended so the runner's timeout can trigger.
				await new Promise<void>((_, reject) => {
					const signal = options?.abortController?.signal;
					if (signal?.aborted === true) {
						reject(new Error("aborted"));
						return;
					}
					signal?.addEventListener("abort", () => {
						reject(new Error("aborted"));
					});
				});
				return;
			}
			if (outcome.kind === "error") {
				yield {
					type: "result",
					subtype: outcome.subtype,
					duration_ms: 10,
					duration_api_ms: 10,
					is_error: true,
					num_turns: 1,
					stop_reason: null,
					total_cost_usd: 0,
					usage: {
						input_tokens: 0,
						output_tokens: 0,
					},
					modelUsage: {},
					permission_denials: [],
					errors: [...outcome.errors],
					uuid: "00000000-0000-0000-0000-000000000000",
					session_id: "test",
				} as unknown as SDKMessage;
				return;
			}
			yield {
				type: "result",
				subtype: "success",
				duration_ms: 10,
				duration_api_ms: 10,
				is_error: false,
				num_turns: 1,
				result: outcome.text,
				stop_reason: "end_turn",
				total_cost_usd: outcome.costUsd ?? 0.01,
				usage: {
					input_tokens: 5,
					output_tokens: outcome.tokensUsed ?? 42,
				},
				modelUsage: {},
				permission_denials: [],
				uuid: "00000000-0000-0000-0000-000000000000",
				session_id: "test",
			} as unknown as SDKMessage;
		})();
		// Query has interrupt / setPermissionMode / etc. on the interface. For
		// tests, we only use the async iterator — cast through the minimum
		// needed to satisfy the type.
		return gen as unknown as Query;
	};
}

const defaultSubagents: Record<string, SubagentDefinition> = {
	main: { model: "claude-sonnet-4-6", maxTurns: 4, systemPromptPrefix: "You are Theo." },
	consolidator: {
		model: "claude-haiku-4-6",
		maxTurns: 3,
		systemPromptPrefix: "You consolidate memory.",
	},
	scanner: {
		model: "claude-haiku-4-6",
		maxTurns: 2,
		systemPromptPrefix: "You scan for commitments.",
	},
	reflector: {
		model: "claude-sonnet-4-6",
		maxTurns: 3,
		systemPromptPrefix: "You reflect on the week.",
	},
};

// Helper: count emitted events of a given type in the event log.
async function countEvents(type: string): Promise<number> {
	const rows = await pool.sql<{ count: string }[]>`
		SELECT COUNT(*)::text AS count FROM events WHERE type = ${type}
	`;
	return Number(rows[0]?.count ?? 0);
}

async function eventDataFor(type: string): Promise<ReadonlyArray<Record<string, unknown>>> {
	const rows = await pool.sql<{ data: Record<string, unknown> }[]>`
		SELECT data FROM events WHERE type = ${type} ORDER BY id ASC
	`;
	return rows.map((r) => r.data);
}

async function waitIdle(scheduler: Scheduler): Promise<void> {
	while (scheduler.activeCount() > 0) {
		await new Promise((r) => setTimeout(r, 10));
	}
}

function makeScheduler(
	overrides: {
		builtins?: readonly BuiltinJobSeed[];
		subagents?: Record<string, SubagentDefinition>;
		queryFn?: ReturnType<typeof stubQueryFn>;
		now?: () => Date;
		tickIntervalMs?: number;
		maxConcurrent?: number;
	} = {},
): Scheduler {
	return new Scheduler({
		store,
		bus,
		memoryServer: stubMemoryServer,
		subagents: overrides.subagents ?? defaultSubagents,
		builtins: overrides.builtins ?? [],
		queryFn: overrides.queryFn ?? stubQueryFn({ kind: "success", text: "all good" }),
		...(overrides.now !== undefined ? { now: overrides.now } : {}),
		config: {
			tickIntervalMs: overrides.tickIntervalMs ?? 60_000,
			maxConcurrent: overrides.maxConcurrent ?? 1,
		},
	});
}

// ---------------------------------------------------------------------------
// Seeding / startup
// ---------------------------------------------------------------------------

describe("Scheduler.start — built-in seeding", () => {
	test("seeds every built-in job on first start", async () => {
		const scheduler = makeScheduler({
			builtins: [
				{
					name: "consolidation",
					cron: "0 */6 * * *",
					agent: "consolidator",
					prompt: "consolidate",
					maxDurationMs: 60_000,
					maxBudgetUsd: 0.05,
				},
				{
					name: "scan",
					cron: "0 9 * * *",
					agent: "scanner",
					prompt: "scan",
					maxDurationMs: 30_000,
					maxBudgetUsd: 0.05,
				},
			],
			now: () => new Date(Date.UTC(2026, 0, 15, 0, 0, 0)),
		});
		await scheduler.start();
		await scheduler.stop();

		const consolidation = await store.getByName("consolidation");
		const scan = await store.getByName("scan");
		expect(consolidation).not.toBeNull();
		expect(scan).not.toBeNull();
	});

	test("seeding emits job.created events", async () => {
		const scheduler = makeScheduler({
			builtins: [
				{
					name: "seeded",
					cron: "0 */6 * * *",
					agent: "consolidator",
					prompt: "seed",
					maxDurationMs: 60_000,
					maxBudgetUsd: 0.05,
				},
			],
			now: () => new Date(Date.UTC(2026, 0, 15, 0, 0, 0)),
		});
		await scheduler.start();
		await scheduler.stop();
		await bus.flush();
		expect(await countEvents("job.created")).toBe(1);
	});

	test("does not duplicate built-ins on restart", async () => {
		const seed: BuiltinJobSeed = {
			name: "consolidation",
			cron: "0 */6 * * *",
			agent: "consolidator",
			prompt: "consolidate",
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.05,
		};
		const first = makeScheduler({
			builtins: [seed],
			now: () => new Date(Date.UTC(2026, 0, 15, 0, 0, 0)),
		});
		await first.start();
		await first.stop();
		const second = makeScheduler({
			builtins: [seed],
			now: () => new Date(Date.UTC(2026, 0, 15, 0, 0, 0)),
		});
		await second.start();
		await second.stop();

		const all = await store.list();
		expect(all.filter((j) => j.name === "consolidation").length).toBe(1);
	});
});

// ---------------------------------------------------------------------------
// Tick execution
// ---------------------------------------------------------------------------

describe("Scheduler.tick — execution", () => {
	test("tick executes a due job and emits triggered + completed + notification", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({
				kind: "success",
				text: "Found an expiring token tomorrow",
				tokensUsed: 100,
				costUsd: 0.02,
			}),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});

		const input = {
			id: newJobId(),
			name: "test-job",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "do it",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();

		expect(await countEvents("job.triggered")).toBe(1);
		expect(await countEvents("job.completed")).toBe(1);
		expect(await countEvents("notification.created")).toBe(1);

		const completedData = (await eventDataFor("job.completed"))[0];
		expect(completedData?.["jobName"]).toBe("test-job");
		expect(completedData?.["tokensUsed"]).toBe(100);
		expect(completedData?.["costUsd"]).toBeCloseTo(0.02);

		const notificationData = (await eventDataFor("notification.created"))[0];
		expect(notificationData?.["source"]).toBe("test-job");
		expect(notificationData?.["body"]).toContain("Found an expiring token");
	});

	test("tick skips a job that is already running", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "hang" }),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "slow-job",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "hang",
			enabled: true,
			maxDurationMs: 200, // triggers abort after 200ms
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		// First tick launches but the job hangs.
		await scheduler.tick();
		expect(scheduler.activeCount()).toBe(1);

		// Second tick should see the job as active and skip it.
		await scheduler.tick();
		expect(scheduler.activeCount()).toBe(1);

		// Wait for the hang to abort + resolve.
		await waitIdle(scheduler);
		await bus.flush();

		expect(await countEvents("job.triggered")).toBe(1);
		expect(await countEvents("job.failed")).toBe(1);
	});

	test("tick skips disabled jobs", async () => {
		const input = {
			id: newJobId(),
			name: "inert",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "inert",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);
		await store.disable(input.id);

		const scheduler = makeScheduler({
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		await scheduler.tick();
		await bus.flush();
		expect(await countEvents("job.triggered")).toBe(0);
	});

	test("tick respects maxConcurrent=1 when multiple jobs are due", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "hang" }),
			maxConcurrent: 1,
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		for (const name of ["a", "b"]) {
			await store.create({
				id: newJobId(),
				name,
				cron: "0 */6 * * *",
				agent: "main",
				prompt: name,
				enabled: true,
				maxDurationMs: 100,
				maxBudgetUsd: 0.1,
				nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
			});
		}

		await scheduler.tick();
		expect(scheduler.activeCount()).toBe(1);
		await waitIdle(scheduler);
	});
});

// ---------------------------------------------------------------------------
// Overdue handling
// ---------------------------------------------------------------------------

describe("Scheduler.start — overdue handling", () => {
	test("overdue jobs run once on start", async () => {
		let calls = 0;
		const scheduler = makeScheduler({
			queryFn: (params) => {
				calls++;
				return stubQueryFn({ kind: "success", text: "done" })(params);
			},
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		await store.create({
			id: newJobId(),
			name: "overdue-job",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "catch up",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 11, 0, 0)), // 2h overdue
		});

		await scheduler.start();
		await waitIdle(scheduler);
		await scheduler.stop();
		await bus.flush();

		expect(calls).toBe(1);
		expect(await countEvents("job.triggered")).toBe(1);
	});
});

// ---------------------------------------------------------------------------
// One-off cleanup
// ---------------------------------------------------------------------------

describe("Scheduler one-off cleanup", () => {
	test("one-off job is disabled after execution", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "success", text: "done" }),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "one-off",
			cron: null,
			agent: "main",
			prompt: "once",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();

		const fetched = await store.getById(input.id);
		expect(fetched?.enabled).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// Failure paths
// ---------------------------------------------------------------------------

describe("Scheduler failure handling", () => {
	test("thrown error in query marks execution failed and emits job.failed", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "throw", message: "sdk exploded" }),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "explodes",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "boom",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();

		expect(await countEvents("job.failed")).toBe(1);
		const failedData = (await eventDataFor("job.failed"))[0];
		expect(failedData?.["message"]).toContain("sdk exploded");

		const execs = await store.listExecutions(input.id, 5);
		expect(execs[0]?.status).toBe("failed");
		expect(execs[0]?.errorMessage).toContain("sdk exploded");
	});

	test("SDK result error subtype (max_turns) surfaces as job.failed", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({
				kind: "error",
				subtype: "error_max_turns",
				errors: ["too many turns"],
			}),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "ran-out",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "long",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();
		expect(await countEvents("job.failed")).toBe(1);
	});

	test("unknown subagent surfaces as job.failed", async () => {
		const scheduler = makeScheduler({
			subagents: {}, // no agents registered
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "orphan",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();
		expect(await countEvents("job.failed")).toBe(1);
		const data = (await eventDataFor("job.failed"))[0];
		expect(data?.["message"]).toContain("Unknown subagent");
	});

	test("maxDurationMs timeout aborts the turn and marks it failed", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "hang" }),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "timeout",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "hang forever",
			enabled: true,
			maxDurationMs: 75,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);
		await bus.flush();

		expect(await countEvents("job.failed")).toBe(1);
	});
});

// ---------------------------------------------------------------------------
// Schedule advancement
// ---------------------------------------------------------------------------

describe("Scheduler schedule advancement", () => {
	test("next_run_at advances after a successful cron run", async () => {
		const scheduler = makeScheduler({
			queryFn: stubQueryFn({ kind: "success", text: "done" }),
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "cron-advance",
			cron: "0 */6 * * *",
			agent: "main",
			prompt: "x",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.1,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);

		const fetched = await store.getById(input.id);
		// Next 0/6 after 13:00 is 18:00 UTC.
		expect(fetched?.nextRunAt.toISOString()).toBe("2026-01-15T18:00:00.000Z");
		expect(fetched?.lastRunAt).not.toBeNull();
	});
});

// ---------------------------------------------------------------------------
// Subagent selection
// ---------------------------------------------------------------------------

describe("Scheduler subagent selection", () => {
	test("runAgentTurn uses the correct subagent model + maxTurns", async () => {
		let capturedOptions: Options | undefined;
		const scheduler = makeScheduler({
			queryFn: (params) => {
				capturedOptions = params.options;
				return stubQueryFn({ kind: "success", text: "ok" })(params);
			},
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "uses-consolidator",
			cron: "0 */6 * * *",
			agent: "consolidator",
			prompt: "compact",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.05,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);

		expect(capturedOptions?.model).toBe("claude-haiku-4-6");
		expect(capturedOptions?.maxTurns).toBe(3);
		expect(capturedOptions?.maxBudgetUsd).toBeCloseTo(0.05);
		expect(capturedOptions?.allowedTools).toContain("mcp__memory__*");
	});

	test("advisorModel on a subagent is forwarded via options.settings.advisorModel", async () => {
		let capturedOptions: Options | undefined;
		const subagents: Record<string, SubagentDefinition> = {
			reflector: {
				model: "claude-sonnet-4-6",
				maxTurns: 3,
				systemPromptPrefix: "You reflect.",
				advisorModel: "claude-opus-4-6",
			},
		};
		const scheduler = makeScheduler({
			subagents,
			queryFn: (params) => {
				capturedOptions = params.options;
				return stubQueryFn({ kind: "success", text: "ok" })(params);
			},
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "uses-reflector",
			cron: "0 3 * * 0",
			agent: "reflector",
			prompt: "reflect",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.05,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);

		const settings = capturedOptions?.settings;
		expect(settings).toBeDefined();
		if (settings !== undefined && typeof settings === "object") {
			expect((settings as { advisorModel?: string }).advisorModel).toBe("claude-opus-4-6");
		}
	});

	test("subagent without advisorModel omits settings entirely", async () => {
		let capturedOptions: Options | undefined;
		const scheduler = makeScheduler({
			queryFn: (params) => {
				capturedOptions = params.options;
				return stubQueryFn({ kind: "success", text: "ok" })(params);
			},
			now: () => new Date(Date.UTC(2026, 0, 15, 13, 0, 0)),
		});
		const input = {
			id: newJobId(),
			name: "reflex-scanner",
			cron: "0 9 * * *",
			agent: "scanner",
			prompt: "scan",
			enabled: true,
			maxDurationMs: 60_000,
			maxBudgetUsd: 0.05,
			nextRunAt: new Date(Date.UTC(2026, 0, 15, 12, 0, 0)),
		};
		await store.create(input);

		await scheduler.tick();
		await waitIdle(scheduler);

		expect(capturedOptions?.settings).toBeUndefined();
	});
});
