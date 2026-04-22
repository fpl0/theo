/**
 * Goals MCP tools — `read_goals` and `record_goal`.
 *
 * `read_goals`: lists active goals visible at the caller's effective trust
 * tier. The chat engine threads `effectiveTrust` into tool metadata
 * (Phase 10); the tool filters by that tier to prevent webhook-trust turns
 * from seeing owner_confirmed goals (foundation.md §7.3). Redacted goals
 * appear with title + status but body masked.
 *
 * `record_goal`: creates a new goal with origin = "owner" (or whatever the
 * caller's trust tier implies). Without this tool, goal creation could only
 * happen via internal code paths — the SDK had no way to persist an owner's
 * stated intent, so "I want to set a goal: …" turns went into the episodic
 * log and nowhere else. The tool is trust-scoped: only an `owner` or
 * `owner_confirmed` turn may record goals; other tiers get a clear refusal.
 */

import { tool } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { errorResult } from "../mcp/tool-helpers.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import type { GoalRepository } from "./repository.ts";
import type { GoalState, GoalStatus } from "./types.ts";

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const STATUS_ENUM = [
	"proposed",
	"active",
	"blocked",
	"paused",
	"completed",
	"cancelled",
	"quarantined",
	"expired",
] as const satisfies readonly GoalStatus[];

function renderGoal(goal: GoalState, title: string | null, includePlan: boolean): string {
	const redactedTag = goal.redacted ? " [redacted]" : "";
	const titlePart = title === null || goal.redacted ? "" : `: ${title}`;
	const header =
		`Goal #${String(goal.nodeId)}${titlePart} (${goal.status}, priority ${String(goal.ownerPriority)}, ` +
		`trust ${goal.effectiveTrust}, origin ${goal.origin})${redactedTag}`;
	const planPart =
		includePlan && !goal.redacted && goal.plan.length > 0
			? `\n  Plan v${String(goal.planVersion)}: ` +
				goal.plan.map((step, i) => `${String(i + 1)}. [${step.taskId}] ${step.body}`).join("\n    ")
			: "";
	return header + planPart;
}

// ---------------------------------------------------------------------------
// Tool factory
// ---------------------------------------------------------------------------

export interface ReadGoalsDeps {
	readonly goals: GoalRepository;
	/**
	 * Function that reads the caller's effective trust tier from the SDK
	 * tool `extra` object. Phase 10 threads this via `metadata.goalEffectiveTrust`
	 * or an equivalent hook; tests can inject a constant.
	 */
	readonly resolveTrust: (extra: unknown) => TrustTier;
}

/**
 * Minimum trust tier allowed to create goals via `record_goal`. Lower tiers
 * (verified/inferred/external/untrusted) would let a webhook-originated turn
 * fabricate commitments in the owner's name — foundation.md §7.3 forbids
 * this without an explicit proposal + approval flow.
 */
function canRecordGoal(tier: TrustTier): boolean {
	return tier === "owner" || tier === "owner_confirmed";
}

export function recordGoalTool(deps: ReadGoalsDeps) {
	return tool(
		"record_goal",
		'Record a goal the owner has stated (e.g. "I want to draft a summary by Friday"). ' +
			"Use this whenever the owner expresses a commitment, intent, or durable objective. " +
			"Pick a short imperative `title` and a 1-2 sentence `description` capturing the " +
			"what/why/deadline. `ownerPriority` defaults to 50 on a 0-100 scale; use higher " +
			"values for stated-important goals and lower for aspirational ones. Only call this " +
			"at trust tier owner or owner_confirmed — lower tiers are refused.",
		{
			title: z.string().min(1).max(200),
			description: z.string().min(1).max(2000),
			ownerPriority: z.number().int().min(0).max(100).default(50),
		},
		async ({ title, description, ownerPriority }, extra) => {
			try {
				const trust = deps.resolveTrust(extra);
				if (!canRecordGoal(trust)) {
					return {
						content: [
							{
								type: "text",
								text:
									`Refused: record_goal requires trust tier owner or owner_confirmed; ` +
									`this turn runs at ${trust}. Ask the owner to confirm.`,
							},
						],
					};
				}
				const state = await deps.goals.create({
					title,
					description,
					origin: "owner",
					ownerPriority,
					effectiveTrust: trust,
					actor: "user",
				});
				return {
					content: [
						{
							type: "text",
							text:
								`Recorded goal #${String(state.nodeId)}: ${title} ` +
								`(status=${state.status}, priority=${String(state.ownerPriority)}, trust=${state.effectiveTrust}).`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function readGoalsTool(deps: ReadGoalsDeps) {
	return tool(
		"read_goals",
		"Read active goals visible at your current trust tier. " +
			"Returns the goal list with status, priority, and trust tier. " +
			"Set includePlan=true to see the plan steps. " +
			"Redacted goals appear with body masked.",
		{
			status: z.array(z.enum(STATUS_ENUM)).optional(),
			includePlan: z.boolean().default(false),
		},
		async ({ status, includePlan }, extra) => {
			try {
				const trust = deps.resolveTrust(extra);
				const filter: Parameters<GoalRepository["listByTrust"]>[1] =
					status !== undefined ? { statuses: status } : {};
				const goals = await deps.goals.listByTrust(trust, filter);
				if (goals.length === 0) {
					return { content: [{ type: "text", text: "No goals visible at your trust tier." }] };
				}
				const titles = await Promise.all(goals.map((g) => deps.goals.readTitle(g.nodeId)));
				const text = goals
					.map((g, i) => renderGoal(g, titles[i] ?? null, includePlan))
					.join("\n\n");
				return { content: [{ type: "text", text }] };
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}
