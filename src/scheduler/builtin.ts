/**
 * Built-in job catalog. Seeded by `Scheduler.start()` if no job with the same
 * name exists yet. Every cron expression is UTC-interpreted (cron-parser
 * defaults to UTC unless `tz` is supplied).
 *
 * Agents referenced here (`consolidator`, `reflector`, `scanner`, `main`)
 * must be present in the `SubagentDefinition` map handed to the scheduler.
 * Phase 14 will provide the full catalog; until then, tests can stub
 * whichever agents they exercise.
 */

import type { BuiltinJobSeed } from "./types.ts";

/**
 * The authoritative list of built-in jobs. Seeded once per fresh database,
 * then left alone — owners can disable or customize them afterwards without
 * the scheduler resurrecting them, because seeding is gated on `getByName`.
 */
export const BUILTIN_JOBS: readonly BuiltinJobSeed[] = [
	{
		name: "consolidation",
		cron: "0 */6 * * *", // every 6 hours on the hour
		agent: "consolidator",
		prompt:
			"Review recent episodes and knowledge graph. Compress old episodes into " +
			"summaries, deduplicate similar nodes, capture projection snapshots.",
		maxDurationMs: 600_000, // 10 min
		maxBudgetUsd: 0.15,
	},
	{
		name: "reflection",
		cron: "0 3 * * 0", // Sunday 03:00 UTC
		agent: "reflector",
		prompt:
			"Analyze behavioral patterns from the past week. Update self-model " +
			"calibration. Note any recurring themes or shifts.",
		maxDurationMs: 300_000, // 5 min
		maxBudgetUsd: 0.1,
	},
	{
		name: "proactive-scan",
		cron: "0 9 * * *", // daily 09:00 UTC
		agent: "scanner",
		prompt:
			"Surface any forgotten commitments, pending follow-ups, or upcoming " +
			"deadlines from memory. Create notifications for anything time-sensitive.",
		maxDurationMs: 180_000, // 3 min
		maxBudgetUsd: 0.08,
	},
	{
		name: "goal-execution",
		cron: "0 10 * * 1-5", // weekdays 10:00 UTC
		agent: "main",
		prompt:
			"Review active goals. Make autonomous progress on any goal that has a " +
			"clear next step. Record what was done.",
		maxDurationMs: 600_000, // 10 min
		maxBudgetUsd: 0.15,
	},
];
