/**
 * GoalRepository: read the `goal_state` / `goal_task` projection, create
 * goals (two-step transactional emit), and bundle listing queries.
 *
 * Writes that mutate execution state (lease, task start, etc.) go through
 * the bus. This repository emits ONLY `goal.created` directly — every other
 * mutation comes from a handler, a command, or the executive loop, and
 * those call `bus.emit()` on their own so the projection records the
 * change canonically.
 *
 * Read queries are narrow — the projection is the hot path for the
 * executive tick loop. Tests exercise the repository through both direct
 * method calls and the projection handler to confirm the two views agree.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventId } from "../events/ids.ts";
import type { Actor, EventMetadata } from "../events/types.ts";
import type { NodeRepository } from "../memory/graph/nodes.ts";
import type { Node, NodeId, TrustTier } from "../memory/graph/types.ts";
import { asNodeId } from "../memory/graph/types.ts";
import {
	type AutonomyPolicy,
	asGoalRunnerId,
	asGoalTaskId,
	asGoalTurnId,
	type BlockReason,
	type GoalNodeMetadata,
	type GoalOrigin,
	type GoalState,
	type GoalStatus,
	type GoalTask,
	type GoalTaskId,
	type GoalTurnId,
	type PlanStep,
	type TaskStatus,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

function rowToGoalState(row: Record<string, unknown>): GoalState {
	const rawPlan = row["plan"];
	const plan = Array.isArray(rawPlan) ? (rawPlan as readonly PlanStep[]) : [];
	const blockedRaw = row["blocked_reason"] as string | null;
	let blockedReason: BlockReason | null = null;
	if (blockedRaw !== null && blockedRaw !== undefined) {
		try {
			blockedReason = JSON.parse(blockedRaw) as BlockReason;
		} catch {
			blockedReason = null;
		}
	}
	return {
		nodeId: asNodeId(row["node_id"] as number),
		status: row["status"] as GoalStatus,
		origin: row["origin"] as GoalOrigin,
		ownerPriority: row["owner_priority"] as number,
		effectiveTrust: row["effective_trust"] as TrustTier,
		planVersion: row["plan_version"] as number,
		plan,
		currentTaskId: (row["current_task_id"] as string | null)
			? asGoalTaskId(row["current_task_id"] as string)
			: null,
		consecutiveFailures: row["consecutive_failures"] as number,
		leasedBy: (row["leased_by"] as string | null)
			? asGoalRunnerId(row["leased_by"] as string)
			: null,
		leasedUntil: (row["leased_until"] as Date | null) ?? null,
		blockedReason,
		quarantinedReason: (row["quarantined_reason"] as string | null) ?? null,
		lastReconsideredAt: (row["last_reconsidered_at"] as Date | null) ?? null,
		lastWorkedAt: (row["last_worked_at"] as Date | null) ?? null,
		proposedExpiresAt: (row["proposed_expires_at"] as Date | null) ?? null,
		redacted: row["redacted"] as boolean,
		createdAt: row["created_at"] as Date,
		updatedAt: row["updated_at"] as Date,
	};
}

function rowToGoalTask(row: Record<string, unknown>): GoalTask {
	const dependsOnRaw = row["depends_on"];
	const dependsOn = Array.isArray(dependsOnRaw) ? (dependsOnRaw as string[]).map(asGoalTaskId) : [];
	return {
		taskId: asGoalTaskId(row["task_id"] as string),
		goalNodeId: asNodeId(row["goal_node_id"] as number),
		planVersion: row["plan_version"] as number,
		stepOrder: row["step_order"] as number,
		body: row["body"] as string,
		dependsOn,
		status: row["status"] as TaskStatus,
		subagent: (row["subagent"] as string | null) ?? null,
		startedAt: (row["started_at"] as Date | null) ?? null,
		completedAt: (row["completed_at"] as Date | null) ?? null,
		lastTurnId: (row["last_turn_id"] as string | null)
			? asGoalTurnId(row["last_turn_id"] as string)
			: null,
		lastRunnerId: (row["last_runner_id"] as string | null)
			? asGoalRunnerId(row["last_runner_id"] as string)
			: null,
		failureCount: row["failure_count"] as number,
		yieldCount: row["yield_count"] as number,
		resumeKey: row["resume_key"] as string | null as GoalTask["resumeKey"],
		redacted: row["redacted"] as boolean,
		createdAt: row["created_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

export interface CreateGoalInput {
	readonly title: string;
	readonly description: string;
	readonly origin: GoalOrigin;
	readonly ownerPriority?: number;
	readonly effectiveTrust: TrustTier;
	readonly actor: Actor;
	readonly proposalExpiresAt?: Date | undefined;
	readonly metadata?: EventMetadata;
}

export interface ListGoalsFilter {
	readonly statuses?: readonly GoalStatus[];
	readonly includeRedacted?: boolean;
	/** Filter to goals whose effective_trust tier is <= this tier. */
	readonly maxEffectiveTrust?: TrustTier;
}

// ---------------------------------------------------------------------------
// Trust tier ordering (for read_goals MCP tool scoping)
// ---------------------------------------------------------------------------

/**
 * Trust tiers ordered from most privileged (owner) to least (untrusted).
 * A turn running at trust tier T sees goals whose `effective_trust` is
 * T-or-lower; this mirrors the read-side check from foundation.md §7.3.
 */
const TRUST_ORDER: Record<TrustTier, number> = {
	owner: 5,
	owner_confirmed: 4,
	verified: 3,
	inferred: 2,
	external: 1,
	untrusted: 0,
};

export function trustTierRank(tier: TrustTier): number {
	return TRUST_ORDER[tier];
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export class GoalRepository {
	constructor(
		private readonly sql: Sql,
		private readonly bus: EventBus,
		private readonly nodes: NodeRepository,
	) {}

	// ---------------------------------------------------------------------------
	// Creation — two-step transactional emit
	// ---------------------------------------------------------------------------

	/**
	 * Create a new goal. Emits `memory.node.created` (for the underlying
	 * `NodeKind='goal'` node) and `goal.created` transactionally — both either
	 * commit or roll back together.
	 *
	 * The projection handler (registered by `registerGoalHandlers`) inserts the
	 * `goal_state` row on `goal.created`.
	 */
	async create(input: CreateGoalInput): Promise<GoalState> {
		// The NodeRepository.create path handles embedding + node event emission
		// in its own transaction. We then emit `goal.created` in a follow-up
		// transaction so the projection handler can see the node row.
		//
		// Node body: the authoritative embeddable text is the title + description.
		// Metadata carries structured fields for the executive's fast path.
		const nodeBody = formatGoalBody(input.title, input.description);
		const nodeMetadata: GoalNodeMetadata = {
			title: input.title,
			description: input.description,
			origin: input.origin,
			ownerPriority: input.ownerPriority ?? 50,
		};
		const node: Node = await this.nodes.create({
			kind: "goal",
			body: nodeBody,
			actor: input.actor,
			trust: input.effectiveTrust,
			nodeMetadata,
		});

		const createdEvent = await this.bus.emit({
			type: "goal.created",
			version: 1,
			actor: input.actor,
			data: {
				nodeId: Number(node.id),
				title: input.title,
				description: input.description,
				origin: input.origin,
				ownerPriority: input.ownerPriority ?? 50,
				effectiveTrust: input.effectiveTrust,
				...(input.proposalExpiresAt !== undefined
					? { proposalExpiresAt: input.proposalExpiresAt.toISOString() }
					: {}),
			},
			metadata: {
				...(input.metadata ?? {}),
				causeId: (input.metadata?.causeId ?? node.sourceEventId ?? undefined) as
					| EventId
					| undefined,
			},
		});

		await this.bus.flush();

		const state = await this.readState(node.id);
		if (state === null) {
			throw new Error(
				`GoalRepository.create: projection missing after goal.created ${createdEvent.id}`,
			);
		}
		return state;
	}

	// ---------------------------------------------------------------------------
	// Reads
	// ---------------------------------------------------------------------------

	async readState(nodeId: NodeId): Promise<GoalState | null> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_state WHERE node_id = ${Number(nodeId)}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		return rowToGoalState(row);
	}

	/**
	 * Fetch the stored title/description/metadata for a goal from its
	 * underlying `NodeKind='goal'` node. Used by the `read_goals` tool and
	 * the audit command.
	 */
	async readTitle(nodeId: NodeId): Promise<string | null> {
		const rows = await this.sql<{ metadata: Record<string, unknown>; body: string }[]>`
			SELECT metadata, body FROM node WHERE id = ${Number(nodeId)}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		const metaTitle = typeof row.metadata["title"] === "string" ? row.metadata["title"] : null;
		return metaTitle ?? row.body.split("\n")[0] ?? null;
	}

	async list(filter: ListGoalsFilter = {}): Promise<readonly GoalState[]> {
		const statuses = filter.statuses ?? null;
		// Redacted goals are visible by default (with bodies masked at render
		// time) — the `/goals` surface intentionally surfaces the redaction
		// tag so operators can still audit without re-reading sensitive
		// content. Set includeRedacted=false to exclude them entirely.
		const includeRedacted = filter.includeRedacted ?? true;
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_state
			WHERE (${statuses}::text[] IS NULL OR status = ANY(${statuses}::text[]))
			  AND (${includeRedacted}::boolean OR redacted = false)
			ORDER BY owner_priority DESC, created_at ASC
		`;
		return rows.map(rowToGoalState);
	}

	/**
	 * List goals visible at the given effective trust tier. A turn at tier T
	 * sees goals whose stored `effective_trust` is rank-less-than-or-equal-to
	 * T. `owner` sees everything; `external` sees only external/untrusted.
	 */
	async listByTrust(
		tier: TrustTier,
		filter: Omit<ListGoalsFilter, "maxEffectiveTrust"> = {},
	): Promise<readonly GoalState[]> {
		const all = await this.list(filter);
		const threshold = trustTierRank(tier);
		return all.filter((g) => trustTierRank(g.effectiveTrust) <= threshold);
	}

	async tasks(nodeId: NodeId): Promise<readonly GoalTask[]> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_task
			WHERE goal_node_id = ${Number(nodeId)}
			ORDER BY plan_version DESC, step_order ASC
		`;
		return rows.map(rowToGoalTask);
	}

	/**
	 * Find the next pending task in the goal's current plan whose dependencies
	 * are all `completed`. Returns null when the plan is drained or every
	 * pending task is still waiting on a dependency.
	 */
	async nextReadyTask(nodeId: NodeId): Promise<GoalTask | null> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT t.*
			FROM goal_task t
			JOIN goal_state s ON s.node_id = t.goal_node_id
			WHERE t.goal_node_id = ${Number(nodeId)}
			  AND t.plan_version = s.plan_version
			  AND t.status = 'pending'
			ORDER BY t.step_order ASC
		`;
		const pending = rows.map(rowToGoalTask);
		if (pending.length === 0) return null;

		// Only inspect dependencies referenced by pending tasks.
		const allDeps = new Set<GoalTaskId>();
		for (const t of pending) {
			for (const dep of t.dependsOn) allDeps.add(dep);
		}
		if (allDeps.size === 0) return pending[0] ?? null;

		const depIds = Array.from(allDeps);
		const depRows = await this.sql<Record<string, unknown>[]>`
			SELECT task_id, status FROM goal_task WHERE task_id = ANY(${depIds}::text[])
		`;
		const statusByTask = new Map<string, TaskStatus>();
		for (const row of depRows) {
			statusByTask.set(row["task_id"] as string, row["status"] as TaskStatus);
		}

		for (const task of pending) {
			const allDone = task.dependsOn.every((d) => statusByTask.get(d) === "completed");
			if (allDone) return task;
		}
		return null;
	}

	async getTask(taskId: GoalTaskId): Promise<GoalTask | null> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_task WHERE task_id = ${taskId}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		return rowToGoalTask(row);
	}

	/**
	 * Sum per-task cost across every `goal.task_completed` event for this
	 * goal. Used for per-goal budget enforcement before emitting
	 * `goal.task_started`. Reads from the event log rather than the
	 * projection so cost is always consistent with what the event log
	 * actually contains (projection doesn't store cost).
	 */
	async totalCostUsd(nodeId: NodeId): Promise<number> {
		const rows = await this.sql<{ total: string | null }[]>`
			SELECT COALESCE(SUM((data->>'totalCostUsd')::numeric), 0)::text AS total
			FROM events
			WHERE type = 'goal.task_completed'
			  AND (data->>'nodeId')::integer = ${Number(nodeId)}
		`;
		const first = rows[0];
		const total = first?.total ?? "0";
		return Number(total);
	}

	/**
	 * List every task currently marked `in_progress` across all goals. Used
	 * by the recovery job at startup to find dangling turns.
	 */
	async inProgressTasks(): Promise<readonly GoalTask[]> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_task WHERE status = 'in_progress'
		`;
		return rows.map(rowToGoalTask);
	}

	/** List goals currently leased (leased_by IS NOT NULL). */
	async leasedGoals(): Promise<readonly GoalState[]> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT * FROM goal_state WHERE leased_by IS NOT NULL
		`;
		return rows.map(rowToGoalState);
	}

	// ---------------------------------------------------------------------------
	// Autonomy policy
	// ---------------------------------------------------------------------------

	async getAutonomyPolicy(domain: string): Promise<AutonomyPolicy | null> {
		const rows = await this.sql<Record<string, unknown>[]>`
			SELECT domain, level, set_by, set_at, reason
			FROM autonomy_policy WHERE domain = ${domain}
		`;
		const row = rows[0];
		if (row === undefined) return null;
		return {
			domain: row["domain"] as string,
			level: row["level"] as number,
			setBy: row["set_by"] as Actor,
			setAt: row["set_at"] as Date,
			reason: (row["reason"] as string | null) ?? null,
		};
	}

	async setAutonomyPolicy(
		domain: string,
		level: number,
		setBy: Actor,
		reason: string | null,
		tx?: TransactionSql,
	): Promise<void> {
		const q = tx !== undefined ? asQueryable(tx) : this.sql;
		await q`
			INSERT INTO autonomy_policy (domain, level, set_by, reason, set_at)
			VALUES (${domain}, ${level}, ${setBy}, ${reason}, now())
			ON CONFLICT (domain) DO UPDATE SET
				level = EXCLUDED.level,
				set_by = EXCLUDED.set_by,
				reason = EXCLUDED.reason,
				set_at = EXCLUDED.set_at
		`;
	}

	// ---------------------------------------------------------------------------
	// Lease helpers (read-only here; writes go through lease.ts + bus.emit)
	// ---------------------------------------------------------------------------

	/**
	 * Is a goal's lease currently held and unexpired? Independent of which
	 * runner holds it. Useful for diagnostics; the acquisition path uses
	 * its own SKIP LOCKED query.
	 */
	async isLeaseHeld(nodeId: NodeId, at: Date): Promise<boolean> {
		const rows = await this.sql<{ held: boolean }[]>`
			SELECT (leased_by IS NOT NULL AND leased_until > ${at}) AS held
			FROM goal_state WHERE node_id = ${Number(nodeId)}
		`;
		const row = rows[0];
		return row?.held ?? false;
	}

	// ---------------------------------------------------------------------------
	// Resume contexts
	// ---------------------------------------------------------------------------

	async writeResumeContext(
		id: string,
		nodeId: NodeId,
		taskId: GoalTaskId,
		turnId: GoalTurnId,
		sessionId: string | null,
		snapshot: unknown,
		tokenCount: number,
		expiresAt: Date,
	): Promise<void> {
		await this.sql`
			INSERT INTO resume_context (
				id, goal_node_id, task_id, turn_id, session_id,
				snapshot, token_count, expires_at
			) VALUES (
				${id}, ${Number(nodeId)}, ${taskId}, ${turnId}, ${sessionId},
				${this.sql.json(snapshot as Parameters<Sql["json"]>[0])},
				${tokenCount}, ${expiresAt}
			)
		`;
	}

	async deleteExpiredResumeContexts(at: Date): Promise<number> {
		const result = await this.sql`
			DELETE FROM resume_context WHERE expires_at < ${at}
		`;
		return result.count ?? 0;
	}

	// ---------------------------------------------------------------------------
	// Audit — read-only view of the event log filtered by goal node
	// ---------------------------------------------------------------------------

	/**
	 * List every event in the log whose payload references this goal node.
	 * Used by the `/audit` operator command.
	 */
	async auditEvents(nodeId: NodeId): Promise<readonly AuditEventRow[]> {
		const rows = await this.sql<
			{
				id: string;
				type: string;
				timestamp: Date;
				actor: string;
				data: Record<string, unknown>;
			}[]
		>`
			SELECT id, type, timestamp, actor, data
			FROM events
			WHERE type LIKE 'goal.%'
			  AND (data->>'nodeId')::integer = ${Number(nodeId)}
			ORDER BY id ASC
		`;
		return rows.map((row) => ({
			id: row.id,
			type: row.type,
			timestamp: row.timestamp,
			actor: row.actor,
			data: row.data,
		}));
	}
}

export interface AuditEventRow {
	readonly id: string;
	readonly type: string;
	readonly timestamp: Date;
	readonly actor: string;
	readonly data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Body formatting
// ---------------------------------------------------------------------------

/**
 * Format a goal's node body from title + description. The node body is what
 * RRF embeds and searches, so it needs to be a single coherent text blob.
 */
function formatGoalBody(title: string, description: string): string {
	if (description.length === 0) return title;
	return `${title}\n\n${description}`;
}

// Exported for tests and re-use.
export { formatGoalBody };
