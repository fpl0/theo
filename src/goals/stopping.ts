/**
 * Stopping criteria / budget enforcement for executive turns.
 *
 * Four independent caps guard against runaway cost:
 *
 *   1. Per-turn `maxTurns` — SDK-level cap on iterations inside a single
 *      subagent call. Enforced by the SDK (`Options.maxTurns`).
 *   2. Per-turn `maxBudgetUsd` — SDK-level dollar cap. Enforced by the SDK.
 *   3. Per-turn `maxDurationMs` — wall-clock cap. Enforced by
 *      AbortController in the executive dispatch.
 *   4. Per-goal `maxGoalCostUsd` — cumulative cost across all task events for
 *      the goal. Enforced BEFORE emitting `goal.task_started` by reading the
 *      event log via `GoalRepository.totalCostUsd`.
 *
 * Advisor iterations are billed at advisor rates (different from executor
 * rates), so `extractTaskCost` sums `usage.iterations[]` rather than reading
 * `total_cost_usd` directly. The SDK's `total_cost_usd` already reflects
 * this post-0.2.9, but keeping the per-iteration breakdown in the log makes
 * replay-based audit trivial.
 */

import type { SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

// ---------------------------------------------------------------------------
// Budget constants
// ---------------------------------------------------------------------------

/** Default per-turn budget for executive dispatches. */
export const DEFAULT_TURN_BUDGET: TurnBudget = {
	maxTurns: 30,
	maxBudgetUsd: 0.5,
	maxDurationMs: 5 * 60_000,
};

/** Default per-goal cumulative cost cap in USD. */
export const DEFAULT_GOAL_BUDGET_USD = 5.0;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TurnBudget {
	readonly maxTurns: number;
	readonly maxBudgetUsd: number;
	readonly maxDurationMs: number;
}

export interface CostBreakdown {
	readonly tokens: number;
	readonly costUsd: number;
}

// ---------------------------------------------------------------------------
// Rate tables
// ---------------------------------------------------------------------------

/**
 * Approximate cost per million tokens for the models Theo uses. Kept inline
 * rather than a config dependency so tests can override via the `rates`
 * parameter below; real dispatch uses the static table and updates it when
 * Anthropic publishes new pricing.
 *
 * Values are USD per million tokens (input / output).
 */
const DEFAULT_RATES: Readonly<Record<string, { input: number; output: number }>> = {
	// Anthropic Sonnet tier (input / output USD per million tokens)
	"claude-sonnet-4-6": { input: 3.0, output: 15.0 },
	"claude-sonnet-4-5": { input: 3.0, output: 15.0 },
	"claude-sonnet-3-5": { input: 3.0, output: 15.0 },
	// Opus
	"claude-opus-4-6": { input: 15.0, output: 75.0 },
	"claude-opus-4-5": { input: 15.0, output: 75.0 },
	// Haiku
	"claude-haiku-4-5": { input: 0.8, output: 4.0 },
	"claude-haiku-3-5": { input: 0.8, output: 4.0 },
};

/** Aliased SDK names resolved to the current concrete models. */
const ALIAS: Readonly<Record<string, string>> = {
	sonnet: "claude-sonnet-4-6",
	opus: "claude-opus-4-6",
	haiku: "claude-haiku-4-5",
};

function rateFor(
	model: string | undefined,
	rates: Readonly<Record<string, { input: number; output: number }>> = DEFAULT_RATES,
): { input: number; output: number } {
	if (model === undefined) return { input: 0, output: 0 };
	const resolved = ALIAS[model] ?? model;
	return rates[resolved] ?? { input: 0, output: 0 };
}

// ---------------------------------------------------------------------------
// Cost extraction
// ---------------------------------------------------------------------------

/**
 * Sum tokens and cost across `usage.iterations[]` on a successful SDK
 * result. Executor iterations billed at the executor model's rate; advisor
 * iterations (`type === "advisor_message"`) billed at the advisor model's
 * rate.
 *
 * Falls back to `total_cost_usd` when iterations are unavailable (older SDK
 * results or test stubs that don't populate the breakdown).
 *
 * The SDK surfaces snake_case keys (`input_tokens`, `output_tokens`); we
 * access them through an `unknown`-typed bridge to avoid introducing
 * snake_case TS identifiers in Theo's source. The executor `model` is
 * inferred from iteration metadata when available; otherwise falls back to
 * the caller-supplied `defaultModel` (the subagent the executive
 * dispatched to).
 */
export function extractTaskCost(
	result: SDKResultSuccess,
	defaultModel: string | undefined,
	rates: Readonly<Record<string, { input: number; output: number }>> = DEFAULT_RATES,
): CostBreakdown {
	const usage = result.usage as unknown as Record<string, unknown>;
	const iterations = usage["iterations"];
	if (!Array.isArray(iterations) || iterations.length === 0) {
		const input = numberField(usage, "input_tokens");
		const output = numberField(usage, "output_tokens");
		return { tokens: input + output, costUsd: result.total_cost_usd };
	}

	let tokens = 0;
	let costUsd = 0;
	for (const iter of iterations) {
		const rec = iter as Record<string, unknown>;
		const input = numberField(rec, "input_tokens");
		const output = numberField(rec, "output_tokens");
		const cached = numberField(rec, "cache_read_input_tokens");
		const billableInput = Math.max(0, input - cached);
		tokens += input + output;
		const modelName = typeof rec["model"] === "string" ? (rec["model"] as string) : defaultModel;
		const rate = rateFor(modelName, rates);
		costUsd += (billableInput * rate.input) / 1_000_000;
		costUsd += (output * rate.output) / 1_000_000;
	}
	return { tokens, costUsd };
}

function numberField(obj: Record<string, unknown>, key: string): number {
	const v = obj[key];
	return typeof v === "number" ? v : 0;
}

// ---------------------------------------------------------------------------
// Guards
// ---------------------------------------------------------------------------

/**
 * Decide whether this goal has exceeded its per-goal cumulative cost cap.
 * `totalSoFarUsd` is the sum of all prior `goal.task_completed.totalCostUsd`
 * values for this goal.
 */
export function isGoalBudgetExhausted(totalSoFarUsd: number, capUsd: number): boolean {
	return totalSoFarUsd >= capUsd;
}

export function budgetRemaining(totalSoFarUsd: number, capUsd: number): number {
	return Math.max(0, capUsd - totalSoFarUsd);
}
