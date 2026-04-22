/**
 * `read_goals` MCP tool.
 *
 * Reads active goals visible at the caller's effective trust tier. The
 * chat engine threads `effectiveTrust` into tool metadata (Phase 10); this
 * tool filters by that tier to prevent webhook-trust turns from seeing
 * owner_confirmed goals (foundation.md §7.3).
 *
 * Redacted goals appear with title + status but bodies masked. The
 * goal_state row carries the `redacted` flag; the projection sets it on
 * `goal.redacted` and the tool respects it here.
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
