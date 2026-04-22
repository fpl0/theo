/**
 * ExecutiveLoop — one executive turn per invocation.
 *
 * The executive is an **effect handler** (runs only in live mode, skipped
 * during replay). One turn follows the sequence:
 *
 *   1. Acquire a lease on the highest-priority eligible goal.
 *   2. Check the plan; if empty or stale, invoke the planner subagent.
 *   3. Pick the next ready task (dependencies satisfied, pending).
 *   4. Reconsider: should the executive stay committed? If not, yield.
 *   5. Emit `goal.task_started` and dispatch to the subagent.
 *   6. Emit a terminal event (completed / failed / yielded / blocked).
 *   7. Release the lease.
 *
 * Non-determinism (LLM outputs, subagent results) is captured in events
 * first, so the projection rebuilds deterministically from those events.
 *
 * Per-goal budget, poison quarantine, and advisor-aware cost accounting are
 * all enforced inline; see plan §6, §8, and §13.
 */

import type {
	AgentDefinition,
	McpSdkServerConfigWithInstance,
	Options,
	Query,
	SDKResultError,
	SDKResultSuccess,
} from "@anthropic-ai/claude-agent-sdk";
import { query as sdkQuery } from "@anthropic-ai/claude-agent-sdk";
import type { Sql } from "postgres";
import { advisorSettings } from "../chat/subagents.ts";
import { describeError } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import { EXTERNAL_CONTENT_INSTRUCTION } from "../gates/webhooks/envelope.ts";
import { prepareAutonomousEgress, recordCloudEgressTurn } from "../memory/egress.ts";
import type { UserModelRepository } from "../memory/user_model.ts";
import { unrefTimer } from "../util/timers.ts";
import type { GoalLease } from "./lease.ts";
import { shouldReconsider } from "./reconsideration.ts";
import type { GoalRepository } from "./repository.ts";
import {
	DEFAULT_GOAL_BUDGET_USD,
	DEFAULT_TURN_BUDGET,
	extractTaskCost,
	isGoalBudgetExhausted,
	type TurnBudget,
} from "./stopping.ts";
import {
	asGoalTaskId,
	type GoalRunnerId,
	type GoalState,
	type GoalTask,
	newGoalTaskId,
	newGoalTurnId,
	type PlanStep,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Subagent facade
// ---------------------------------------------------------------------------

/**
 * Minimal subagent shape the executive knows about. Mirrors
 * `TheoAgentDefinition` from `src/chat/subagents.ts` but decoupled so tests
 * can stub it without importing the full catalog.
 */
export interface ExecutiveSubagent {
	readonly model: string;
	readonly maxTurns: number;
	readonly systemPromptPrefix: string;
	readonly advisorModel?: string;
}

// ---------------------------------------------------------------------------
// Dispatch types
// ---------------------------------------------------------------------------

export type QueryFn = (params: { prompt: string; options?: Options }) => Query;

export interface DispatchResult {
	readonly kind: "completed" | "failed" | "yielded" | "blocked";
	readonly outcome: string;
	readonly errorClass?:
		| "tool_error"
		| "llm_error"
		| "validation_error"
		| "timeout"
		| "abort"
		| "internal";
	readonly tokens: number;
	readonly costUsd: number;
	readonly artifactIds: readonly string[];
}

// ---------------------------------------------------------------------------
// Planner seam
// ---------------------------------------------------------------------------

/**
 * A planner is any function that, given a goal's current state, returns a
 * new plan. The default planner dispatches to the `planner` subagent via
 * the SDK; tests pass a synthetic planner that returns a deterministic
 * plan without touching the network.
 */
export type Planner = (goal: GoalState) => Promise<readonly PlanStep[]>;

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface ExecutiveDeps {
	readonly bus: EventBus;
	readonly goals: GoalRepository;
	readonly lease: GoalLease;
	readonly memoryServer: McpSdkServerConfigWithInstance;
	readonly subagents: Readonly<Record<string, ExecutiveSubagent>>;
	/**
	 * Full Theo subagent catalog passed to the SDK as `options.agents`. Tests
	 * stub this with an empty object when the planner/coder subagents are
	 * replaced by inline dispatch fns.
	 */
	readonly agents?: Readonly<Record<string, AgentDefinition>>;
	readonly turnBudget?: TurnBudget;
	readonly goalBudgetUsd?: number;
	/** Test seam. Defaults to the real SDK `query()`. */
	readonly queryFn?: QueryFn;
	readonly planner?: Planner;
	readonly now?: () => Date;
	/**
	 * Autonomous cloud-egress dependencies. When both `sql` and `userModel` are
	 * supplied, every cloud-bound subagent dispatch runs the egress consent
	 * check and emits `cloud_egress.turn` for audit. Tests that do not
	 * exercise the egress path omit these.
	 */
	readonly sql?: Sql;
	readonly userModel?: UserModelRepository;
}

export interface ExecutiveContext {
	readonly runnerId: GoalRunnerId;
	/** Whether a higher-priority turn is pending; set by the scheduler. */
	readonly higherPriorityWaiting?: boolean;
	/** Node ids that are part of a recent contradiction detection. */
	readonly contradictingNodeIds?: readonly number[];
	/** True if the goal has an outstanding owner command (pause/priority). */
	readonly pendingOwnerCommand?: boolean;
}

// ---------------------------------------------------------------------------
// Executive loop
// ---------------------------------------------------------------------------

export class ExecutiveLoop {
	private readonly bus: EventBus;
	private readonly goals: GoalRepository;
	private readonly lease: GoalLease;
	private readonly memoryServer: McpSdkServerConfigWithInstance;
	private readonly subagents: Readonly<Record<string, ExecutiveSubagent>>;
	private readonly agents: Readonly<Record<string, AgentDefinition>> | undefined;
	private readonly turnBudget: TurnBudget;
	private readonly goalBudgetUsd: number;
	private readonly queryFn: QueryFn;
	private readonly plannerFn: Planner | null;
	private readonly sql: Sql | null;
	private readonly userModel: UserModelRepository | null;

	constructor(deps: ExecutiveDeps) {
		this.bus = deps.bus;
		this.goals = deps.goals;
		this.lease = deps.lease;
		this.memoryServer = deps.memoryServer;
		this.subagents = deps.subagents;
		this.agents = deps.agents;
		this.turnBudget = deps.turnBudget ?? DEFAULT_TURN_BUDGET;
		this.goalBudgetUsd = deps.goalBudgetUsd ?? DEFAULT_GOAL_BUDGET_USD;
		this.queryFn = deps.queryFn ?? sdkQuery;
		this.plannerFn = deps.planner ?? null;
		this.sql = deps.sql ?? null;
		this.userModel = deps.userModel ?? null;
	}

	/**
	 * Run one executive turn. No-op when no eligible goal is available.
	 * Always releases the lease it acquired, even on failure.
	 */
	async executeOneTurn(ctx: ExecutiveContext): Promise<void> {
		const leased = await this.lease.acquire(ctx.runnerId);
		if (leased === null) return;
		const goal = leased.state;
		try {
			await this.advanceGoal(goal, ctx);
		} finally {
			await this.lease.release(goal.nodeId, ctx.runnerId, "normal");
		}
	}

	// -------------------------------------------------------------------------
	// Internal
	// -------------------------------------------------------------------------

	private async advanceGoal(goal: GoalState, ctx: ExecutiveContext): Promise<void> {
		// 1. Plan check — if the plan is empty, dispatch the planner before picking a task.
		if (goal.plan.length === 0) {
			await this.runPlanner(goal);
			return; // yield; next tick picks up the new plan
		}

		// 2. Pick next ready task. If none, try to complete the goal.
		const task = await this.goals.nextReadyTask(goal.nodeId);
		if (task === null) {
			await this.maybeCompleteGoal(goal);
			return;
		}

		// 3. Reconsideration gate.
		const totalCostUsd = await this.goals.totalCostUsd(goal.nodeId);
		const decision = shouldReconsider({
			goal,
			task,
			higherPriorityWaiting: ctx.higherPriorityWaiting ?? false,
			contradictingNodeIds: ctx.contradictingNodeIds ?? [],
			totalCostUsd,
			goalBudgetUsd: this.goalBudgetUsd,
			turnsSinceLastReview: 0, // tracked externally; refine in a later phase
			pendingOwnerCommand: ctx.pendingOwnerCommand ?? false,
			externalBlocker: goal.blockedReason,
		});
		if (decision.reconsider) {
			await this.bus.emit({
				type: "goal.reconsidered",
				version: 1,
				actor: "theo",
				data: {
					nodeId: Number(goal.nodeId),
					reason: decision.reason,
					outcome: decision.outcome,
				},
				metadata: {},
			});
			if (decision.outcome !== "stay_committed") return;
		}

		// 4. Per-goal budget guard.
		if (isGoalBudgetExhausted(totalCostUsd, this.goalBudgetUsd)) {
			await this.bus.emit({
				type: "goal.blocked",
				version: 1,
				actor: "system",
				data: {
					nodeId: Number(goal.nodeId),
					taskId: task.taskId,
					blocker: { kind: "budget", cap: "goal" },
				},
				metadata: {},
			});
			return;
		}

		// 5. Dispatch the task.
		const turnId = newGoalTurnId();
		const subagentName = task.subagent ?? "planner";
		const taskStarted = await this.bus.emit({
			type: "goal.task_started",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(goal.nodeId),
				taskId: task.taskId,
				turnId,
				runnerId: ctx.runnerId,
				subagent: subagentName,
				maxTurns: this.turnBudget.maxTurns,
				maxBudgetUsd: this.turnBudget.maxBudgetUsd,
				maxDurationMs: this.turnBudget.maxDurationMs,
			},
			metadata: { goalEffectiveTrust: goal.effectiveTrust },
		});

		// Egress consent + audit. Executive turns are autonomous; dispatch is
		// blocked unless autonomous cloud egress is granted. We emit the audit
		// record post-SDK so it captures real token/cost figures.
		let egressDecision: Awaited<ReturnType<typeof prepareAutonomousEgress>> | null = null;
		if (this.sql !== null && this.userModel !== null) {
			egressDecision = await prepareAutonomousEgress({
				sql: this.sql,
				userModel: this.userModel,
				turnClass: "executive",
			});
			if (!egressDecision.allowed) {
				await this.bus.emit({
					type: "goal.task_failed",
					version: 1,
					actor: "theo",
					data: {
						nodeId: Number(goal.nodeId),
						taskId: task.taskId,
						turnId,
						errorClass: "validation_error",
						message: "consent_denied",
						recoverable: false,
					},
					metadata: {},
				});
				return;
			}
		}

		const result = await this.dispatchSubagent(goal, task, subagentName);

		// Cloud-egress audit (when wired). Captures real post-SDK usage.
		if (egressDecision?.allowed) {
			const subagent = this.subagents[subagentName];
			await recordCloudEgressTurn(this.bus, {
				subagent: subagentName,
				model: subagent?.model ?? "unknown",
				...(subagent?.advisorModel !== undefined ? { advisorModel: subagent.advisorModel } : {}),
				inputTokens: Math.round(result.tokens / 2),
				outputTokens: Math.round(result.tokens / 2),
				costUsd: result.costUsd,
				turnClass: "executive",
				causeEventId: taskStarted.id,
				includedDimensions: egressDecision.includedDimensions,
				strippedDimensions: egressDecision.strippedDimensions,
			});
		}

		// 6. Terminal event.
		switch (result.kind) {
			case "completed":
				await this.bus.emit({
					type: "goal.task_completed",
					version: 1,
					actor: "theo",
					data: {
						nodeId: Number(goal.nodeId),
						taskId: task.taskId,
						turnId,
						outcome: result.outcome,
						artifactIds: result.artifactIds,
						totalTokens: result.tokens,
						totalCostUsd: result.costUsd,
					},
					metadata: {},
				});
				break;
			case "failed":
				await this.bus.emit({
					type: "goal.task_failed",
					version: 1,
					actor: "theo",
					data: {
						nodeId: Number(goal.nodeId),
						taskId: task.taskId,
						turnId,
						errorClass: result.errorClass ?? "internal",
						message: result.outcome,
						recoverable: result.errorClass !== "internal",
					},
					metadata: {},
				});
				// Quarantine is owned by the poison breaker (handlers.ts) which
				// reacts to goal.task_failed / goal.task_abandoned.
				break;
			case "yielded":
				await this.bus.emit({
					type: "goal.task_yielded",
					version: 1,
					actor: "theo",
					data: {
						nodeId: Number(goal.nodeId),
						taskId: task.taskId,
						turnId,
						resumeKey: null,
						reason: result.errorClass === "timeout" ? "turn_budget_exceeded" : "preempted",
					},
					metadata: {},
				});
				break;
			case "blocked":
				await this.bus.emit({
					type: "goal.blocked",
					version: 1,
					actor: "system",
					data: {
						nodeId: Number(goal.nodeId),
						taskId: task.taskId,
						blocker: { kind: "external", service: result.outcome },
					},
					metadata: {},
				});
				break;
		}
	}

	private async runPlanner(goal: GoalState): Promise<void> {
		const planner: Planner =
			this.plannerFn ?? ((_g: GoalState): Promise<readonly PlanStep[]> => this.defaultPlanner(_g));
		let plan: readonly PlanStep[];
		try {
			plan = await planner(goal);
		} catch (error) {
			// Planner failures are logged as an abandonment-style signal — we
			// don't have a specific event for "plan_generation_failed", so we
			// mark the goal blocked with a degradation blocker and let the
			// owner intervene.
			await this.bus.emit({
				type: "goal.blocked",
				version: 1,
				actor: "system",
				data: {
					nodeId: Number(goal.nodeId),
					taskId: asGoalTaskId("none"),
					blocker: { kind: "degradation", level: 1 },
				},
				metadata: {},
			});
			console.warn(`Planner failed for goal ${String(goal.nodeId)}: ${describeError(error)}`);
			return;
		}

		if (plan.length === 0) {
			// Empty plan means the goal is already satisfied or has nothing to do.
			await this.maybeCompleteGoal(goal);
			return;
		}

		const previousPlanHash = goal.plan.length === 0 ? null : await hashPlan(goal.plan);
		await this.bus.emit({
			type: "goal.plan_updated",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(goal.nodeId),
				planVersion: goal.planVersion + 1,
				plan,
				reason: goal.plan.length === 0 ? "initial_plan" : "replan",
				previousPlanHash,
			},
			metadata: {},
		});
	}

	/**
	 * Default planner: produce a single "execute" step. Used when no concrete
	 * planner is wired — tests always override this via `deps.planner`.
	 */
	private async defaultPlanner(goal: GoalState): Promise<readonly PlanStep[]> {
		return [
			{
				taskId: newGoalTaskId(),
				body: `Work on goal #${String(goal.nodeId)}`,
				dependsOn: [],
			},
		];
	}

	private async dispatchSubagent(
		goal: GoalState,
		task: GoalTask,
		subagentName: string,
	): Promise<DispatchResult> {
		const subagent = this.subagents[subagentName];
		if (subagent === undefined) {
			return {
				kind: "failed",
				outcome: `Unknown subagent "${subagentName}"`,
				errorClass: "internal",
				tokens: 0,
				costUsd: 0,
				artifactIds: [],
			};
		}

		const controller = new AbortController();
		const timeoutId = setTimeout(() => {
			controller.abort();
		}, this.turnBudget.maxDurationMs);
		unrefTimer(timeoutId);

		try {
			const isExternalTrust =
				goal.effectiveTrust === "external" || goal.effectiveTrust === "untrusted";
			const systemPrompt = [
				// External-trust turns get the content-envelope instruction (§7.6)
				// in front of the subagent prefix so any envelope-wrapped content
				// in the task body is unambiguously data, not instructions.
				isExternalTrust ? EXTERNAL_CONTENT_INSTRUCTION : "",
				subagent.systemPromptPrefix,
				`You are executing task ${String(task.taskId)} for goal ${String(goal.nodeId)}.`,
				`Trust tier: ${goal.effectiveTrust}.`,
				task.body,
			]
				.filter((s) => s.length > 0)
				.join("\n\n");

			// External-trust turns run with a restricted tool allowlist.
			const allowedTools = isExternalTrust
				? ["mcp__memory__search_memory", "mcp__memory__read_core"]
				: ["mcp__memory__*"];

			const settings = advisorSettings(subagent.advisorModel);
			const options: Options = {
				model: subagent.model,
				systemPrompt,
				settingSources: [],
				mcpServers: { memory: this.memoryServer },
				allowedTools,
				maxTurns: Math.min(subagent.maxTurns, this.turnBudget.maxTurns),
				maxBudgetUsd: this.turnBudget.maxBudgetUsd,
				persistSession: false,
				permissionMode: "bypassPermissions",
				allowDangerouslySkipPermissions: true,
				abortController: controller,
				...(this.agents !== undefined ? { agents: this.agents } : {}),
				...(settings !== undefined ? { settings } : {}),
			};

			const generator = this.queryFn({ prompt: task.body, options });

			let responseText = "";
			let successResult: SDKResultSuccess | null = null;
			let failure: SDKResultError | null = null;
			for await (const message of generator) {
				if (message.type !== "result") continue;
				if (message.subtype === "success") {
					successResult = message;
					responseText = message.result;
				} else {
					failure = message;
				}
			}

			if (failure !== null) {
				const errorClass: DispatchResult["errorClass"] =
					failure.subtype === "error_max_turns" || failure.subtype === "error_max_budget_usd"
						? "timeout"
						: failure.subtype === "error_max_structured_output_retries"
							? "validation_error"
							: "internal";
				return {
					kind: errorClass === "timeout" ? "yielded" : "failed",
					outcome: failure.errors.length > 0 ? failure.errors.join("; ") : failure.subtype,
					errorClass,
					tokens: 0,
					costUsd: 0,
					artifactIds: [],
				};
			}

			if (successResult === null) {
				return {
					kind: "failed",
					outcome: "SDK returned no result",
					errorClass: "internal",
					tokens: 0,
					costUsd: 0,
					artifactIds: [],
				};
			}

			const cost = extractTaskCost(successResult, subagent.model);
			return {
				kind: "completed",
				outcome: responseText.slice(0, 500),
				tokens: cost.tokens,
				costUsd: cost.costUsd,
				artifactIds: [],
			};
		} catch (error) {
			if (controller.signal.aborted) {
				return {
					kind: "yielded",
					outcome: "aborted",
					errorClass: "abort",
					tokens: 0,
					costUsd: 0,
					artifactIds: [],
				};
			}
			return {
				kind: "failed",
				outcome: describeError(error),
				errorClass: "internal",
				tokens: 0,
				costUsd: 0,
				artifactIds: [],
			};
		} finally {
			clearTimeout(timeoutId);
		}
	}

	private async maybeCompleteGoal(goal: GoalState): Promise<void> {
		const tasks = await this.goals.tasks(goal.nodeId);
		const currentTasks = tasks.filter((t) => t.planVersion === goal.planVersion);
		if (currentTasks.length === 0) return;
		const allDone = currentTasks.every((t) => t.status === "completed");
		if (!allDone) return;

		const totalCostUsd = await this.goals.totalCostUsd(goal.nodeId);
		await this.bus.emit({
			type: "goal.completed",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(goal.nodeId),
				finalOutcome: `Completed plan v${String(goal.planVersion)}`,
				totalTurns: currentTasks.length,
				totalCostUsd,
			},
			metadata: {},
		});
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function hashPlan(plan: readonly PlanStep[]): Promise<string> {
	const json = JSON.stringify(plan);
	const bytes = new TextEncoder().encode(json);
	const hash = await crypto.subtle.digest("SHA-256", bytes);
	const view = new Uint8Array(hash);
	let out = "";
	for (let i = 0; i < view.length; i++) {
		const byte = view[i] ?? 0;
		out += byte.toString(16).padStart(2, "0");
	}
	return out;
}
