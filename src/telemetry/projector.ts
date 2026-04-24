/**
 * TelemetryProjector — the single bus handler that derives signals from the
 * event stream.
 *
 * This is exhaustive over the `Event` union. Adding a new event type is a
 * compile error here; the projector author picks whether that event needs
 * a metric / log, and the dashboards see it at the next build.
 *
 * Three rules:
 *
 *   1. Exhaustive over `Event`. The `never` default enforces it.
 *   2. No side effects beyond metric / log emission. No DB writes, no
 *      external calls.
 *   3. Duration / cost data lives in the event, not the projector. The
 *      emitter measures; the projector records.
 */

import { assertNever, type Event } from "../events/types.ts";
import {
	asAutonomyDomain,
	asGate,
	asModel,
	asProposalKind,
	asReflexRejectReason,
	asTurnClass,
} from "./labels.ts";
import type { TheoLogger } from "./logger.ts";
import type { MetricsRegistry } from "./metrics.ts";

export interface ProjectorDeps {
	readonly metrics: MetricsRegistry;
	readonly logger: TheoLogger;
}

export class TelemetryProjector {
	constructor(private readonly deps: ProjectorDeps) {}

	/**
	 * Bus handler. Single-dispatch over `event.type`; the `never` default
	 * guarantees compile-time exhaustiveness.
	 */
	handleEvent = async (event: Event): Promise<void> => {
		const m = this.deps.metrics;
		m.eventsAppended.add(1, { event_type: event.type });
		switch (event.type) {
			// -----------------------------------------------------------------
			// Chat
			// -----------------------------------------------------------------
			case "message.received":
				this.deps.logger.log("info", "chat", "message.received", {
					session_id: event.metadata.sessionId,
					gate: event.metadata.gate,
					channel: event.data.channel,
					body: event.data.body,
				});
				return;
			case "turn.started":
				this.deps.logger.log("info", "chat", "turn.started", {
					session_id: event.data.sessionId,
					gate: event.metadata.gate,
					prompt: event.data.prompt,
				});
				return;
			case "turn.completed": {
				const gate = asGate(event.metadata.gate ?? "unknown", "theo.turns.total");
				m.turnCounter.add(1, { gate, status: "ok" });
				m.turnDuration.record(event.data.durationMs, { gate });
				m.inputTokens.add(event.data.inputTokens, { gate });
				m.outputTokens.add(event.data.outputTokens, { gate });
				m.costCounter.add(event.data.costUsd, { gate });
				this.deps.logger.log("info", "chat", "turn.completed", {
					session_id: event.data.sessionId,
					gate: event.metadata.gate,
					response: event.data.responseBody,
					duration_ms: event.data.durationMs,
					input_tokens: event.data.inputTokens,
					output_tokens: event.data.outputTokens,
					total_tokens: event.data.totalTokens,
					cost_usd: event.data.costUsd,
				});
				return;
			}
			case "turn.failed": {
				const gate = asGate(event.metadata.gate ?? "unknown", "theo.turns.total");
				m.turnCounter.add(1, { gate, status: "failed" });
				m.turnDuration.record(event.data.durationMs, { gate });
				this.deps.logger.log("warn", "chat", "turn.failed", {
					session_id: event.data.sessionId,
					gate: event.metadata.gate,
					error_type: event.data.errorType,
					errors: event.data.errors,
					duration_ms: event.data.durationMs,
				});
				return;
			}
			case "session.created":
			case "session.released":
			case "session.compacting":
			case "session.compacted":
				return;

			// -----------------------------------------------------------------
			// Memory
			// -----------------------------------------------------------------
			case "memory.node.created":
			case "memory.node.updated":
			case "memory.edge.created":
			case "memory.edge.expired":
			case "memory.episode.created":
			case "memory.core.updated":
			case "memory.contradiction.detected":
			case "contradiction.requested":
			case "contradiction.classified":
			case "episode.summarize_requested":
			case "episode.summarized":
			case "memory.user_model.updated":
			case "memory.self_model.updated":
			case "memory.skill.created":
			case "memory.skill.promoted":
			case "memory.node.decayed":
			case "memory.pattern.synthesized":
			case "memory.node.merged":
			case "memory.node.importance.propagated":
			case "memory.node.confidence_adjusted":
			case "memory.node.accessed":
				// Memory events flow in high volume — the rate is already captured
				// by `events_appended_total{event_type}`. Keep the projector quiet
				// here; gauges in `gauges.ts` project memory size independently.
				return;

			// -----------------------------------------------------------------
			// Scheduler
			// -----------------------------------------------------------------
			case "job.created":
			case "job.cancelled":
			case "notification.created":
				return;
			case "job.triggered":
				return;
			case "job.completed": {
				m.schedulerTickDuration.record(event.data.durationMs);
				if (event.data.costUsd !== null) {
					m.costCounter.add(event.data.costUsd, { gate: "internal.scheduler" });
				}
				return;
			}
			case "job.failed": {
				m.schedulerTickDuration.record(event.data.durationMs);
				return;
			}

			// -----------------------------------------------------------------
			// System / telemetry
			// -----------------------------------------------------------------
			case "system.started":
				this.deps.logger.info("theo started", { version: event.data.version });
				return;
			case "system.stopped":
				this.deps.logger.info("theo stopped", { reason: event.data.reason });
				return;
			case "system.rollback":
				this.deps.logger.warn("self-update rollback", {
					from: event.data.fromCommit,
					to: event.data.toCommit,
					reason: event.data.reason,
				});
				return;
			case "system.degradation.healed":
			case "self_update.blocked":
			case "synthetic.probe.completed":
				return;
			case "system.handler.dead_lettered": {
				m.handlerErrors.add(1, { handler: event.data.handlerId, reason: "db_error" });
				return;
			}
			case "hook.failed": {
				m.handlerErrors.add(1, { handler: event.data.hookEvent, reason: "unknown" });
				return;
			}

			// -----------------------------------------------------------------
			// Goals (Phase 12a)
			// -----------------------------------------------------------------
			case "goal.created":
			case "goal.confirmed":
			case "goal.priority_changed":
			case "goal.plan_updated":
			case "goal.blocked":
			case "goal.unblocked":
			case "goal.reconsidered":
			case "goal.paused":
			case "goal.resumed":
			case "goal.cancelled":
			case "goal.completed":
			case "goal.quarantined":
			case "goal.redacted":
			case "goal.expired":
				return;
			case "goal.lease_acquired":
			case "goal.lease_released":
				return;
			case "goal.task_started":
				m.taskTurns.add(1, { status: "started" });
				return;
			case "goal.task_progress":
				return;
			case "goal.task_yielded":
				m.taskTurns.add(1, { status: "yielded" });
				return;
			case "goal.task_completed":
				m.taskTurns.add(1, { status: "completed" });
				return;
			case "goal.task_failed":
				m.taskTurns.add(1, { status: "failed" });
				return;
			case "goal.task_abandoned":
				m.taskTurns.add(1, { status: "abandoned" });
				return;

			// -----------------------------------------------------------------
			// Webhooks (Phase 13b)
			// -----------------------------------------------------------------
			case "webhook.received":
				m.reflexReceived.add(1, { source: event.data.source });
				return;
			case "webhook.verified":
				return;
			case "webhook.rejected": {
				const reason = asReflexRejectReason(event.data.reason, "theo.reflex.rejected_total");
				m.reflexRejected.add(1, { source: event.data.source, reason });
				return;
			}
			case "webhook.rate_limited":
				m.reflexRateLimited.add(1, { source: event.data.source });
				return;
			case "webhook.secret_rotated":
			case "webhook.secret_grace_expired":
				return;

			// -----------------------------------------------------------------
			// Reflex (Phase 13b)
			// -----------------------------------------------------------------
			case "reflex.triggered":
				m.reflexDispatched.add(1, { source: event.data.source });
				return;
			case "reflex.thought": {
				const model = asModel(event.data.model, "theo.tokens.input");
				m.inputTokens.add(event.data.inputTokens, { model, role: "executor" });
				m.outputTokens.add(event.data.outputTokens, { model, role: "executor" });
				m.costCounter.add(event.data.costUsd, { model, role: "executor" });
				for (const it of event.data.iterations) {
					const iterModel = asModel(it.model, "theo.tokens.input");
					const role = it.kind === "advisor_message" ? "advisor" : "executor";
					m.inputTokens.add(it.inputTokens, { model: iterModel, role });
					m.outputTokens.add(it.outputTokens, { model: iterModel, role });
					m.costCounter.add(it.costUsd, { model: iterModel, role });
					if (role === "advisor") {
						m.advisorIterations.add(1, { model: iterModel });
						m.advisorCost.add(it.costUsd, { model: iterModel });
					}
				}
				return;
			}
			case "reflex.suppressed":
				return;

			// -----------------------------------------------------------------
			// Ideation (Phase 13b)
			// -----------------------------------------------------------------
			case "ideation.scheduled":
				m.ideationRuns.add(1, { outcome: "scheduled" });
				return;
			case "ideation.proposed": {
				m.ideationProposals.add(1);
				m.ideationCost.add(event.data.costUsd);
				for (const it of event.data.iterations) {
					const iterModel = asModel(it.model, "theo.tokens.input");
					const role = it.kind === "advisor_message" ? "advisor" : "ideation";
					m.inputTokens.add(it.inputTokens, { model: iterModel, role });
					m.outputTokens.add(it.outputTokens, { model: iterModel, role });
					m.costCounter.add(it.costUsd, { model: iterModel, role });
					if (role === "advisor") {
						m.advisorIterations.add(1, { model: iterModel });
						m.advisorCost.add(it.costUsd, { model: iterModel });
					}
				}
				return;
			}
			case "ideation.duplicate_suppressed":
				m.ideationRuns.add(1, { outcome: "duplicate" });
				return;
			case "ideation.budget_exceeded":
				m.ideationRuns.add(1, { outcome: "budget_exceeded" });
				return;
			case "ideation.backoff_extended":
				return;

			// -----------------------------------------------------------------
			// Proposals (Phase 13b)
			// -----------------------------------------------------------------
			case "proposal.requested":
				return;
			case "proposal.drafted":
				return;
			case "proposal.approved":
				m.proposalsApproved.add(1);
				return;
			case "proposal.rejected":
				return;
			case "proposal.executed":
				return;
			case "proposal.expired":
				m.proposalsExpired.add(1);
				return;
			case "proposal.redacted":
				return;

			// -----------------------------------------------------------------
			// Egress (Phase 13b)
			// -----------------------------------------------------------------
			case "policy.autonomous_cloud_egress.enabled":
			case "policy.autonomous_cloud_egress.disabled":
			case "policy.egress_sensitivity.updated":
				return;
			case "cloud_egress.turn": {
				const turnClass = asTurnClass(event.data.turnClass, "theo.cloud_egress.cost_usd_total");
				m.cloudEgressCost.add(event.data.costUsd, { turn_class: turnClass });
				m.cloudEgressTokens.add(event.data.inputTokens + event.data.outputTokens, {
					turn_class: turnClass,
				});
				const model = asModel(event.data.model, "theo.cloud_egress.cost_usd_total");
				m.inputTokens.add(event.data.inputTokens, { model, role: "executor" });
				m.outputTokens.add(event.data.outputTokens, { model, role: "executor" });
				return;
			}

			// -----------------------------------------------------------------
			// Degradation (Phase 13b)
			// -----------------------------------------------------------------
			case "degradation.level_changed":
				// The gauge is observable and read via gauges.ts; we emit a log
				// for operator visibility in real time.
				this.deps.logger.warn("degradation level changed", {
					previous: event.data.previousLevel,
					current: event.data.newLevel,
					reason: event.data.reason,
				});
				return;

			default: {
				// If this errors, a new event type was added without a projector
				// case — the compile error is the forcing function.
				assertNever(event);
				return;
			}
		}
	};

	/** Convenience: emit an autonomy violation. Not derived from a single event
	 *  type — callers signal explicitly when a policy check rejects a write. */
	recordAutonomyViolation(domain: string): void {
		const d = asAutonomyDomain(domain, "theo.autonomy.violations_total");
		this.deps.metrics.autonomyViolations.add(1, { domain: d });
	}

	/** Convenience: emit a proposal-approved increment with a `kind` label. */
	recordProposalApproved(kind: string): void {
		const k = asProposalKind(kind, "theo.proposals.approved_total");
		this.deps.metrics.proposalsApproved.add(1, { kind: k });
	}
}
