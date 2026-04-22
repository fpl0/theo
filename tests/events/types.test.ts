/**
 * Type-level correctness tests for the event type system.
 *
 * These tests verify compile-time invariants: exhaustive switches, branded types,
 * readonly enforcement, and helper extraction types. Runtime assertions confirm
 * that the type narrowing works as expected at execution time.
 */

import { describe, expect, test } from "bun:test";
import type { EventId } from "../../src/events/ids.ts";
import { newEventId } from "../../src/events/ids.ts";
import type {
	EphemeralEvent,
	Event,
	EventData,
	EventOfType,
	NodeUpdate,
	TurnCompletedData,
	TurnFailedData,
} from "../../src/events/types.ts";
import { assertNever } from "../../src/events/types.ts";

describe("Event type system", () => {
	test("assertNever exhaustiveness -- switch on Event['type'] covers all cases", () => {
		// This function must handle every variant of Event["type"].
		// If a variant is added to the union but not handled here, tsc fails.
		function eventLabel(event: Event): string {
			switch (event.type) {
				// Chat
				case "message.received":
					return "message.received";
				case "turn.started":
					return "turn.started";
				case "turn.completed":
					return "turn.completed";
				case "turn.failed":
					return "turn.failed";
				case "session.created":
					return "session.created";
				case "session.released":
					return "session.released";
				case "session.compacting":
					return "session.compacting";
				case "session.compacted":
					return "session.compacted";
				// Memory
				case "memory.node.created":
					return "memory.node.created";
				case "memory.node.updated":
					return "memory.node.updated";
				case "memory.edge.created":
					return "memory.edge.created";
				case "memory.edge.expired":
					return "memory.edge.expired";
				case "memory.episode.created":
					return "memory.episode.created";
				case "memory.core.updated":
					return "memory.core.updated";
				case "memory.contradiction.detected":
					return "memory.contradiction.detected";
				case "contradiction.requested":
					return "contradiction.requested";
				case "contradiction.classified":
					return "contradiction.classified";
				case "episode.summarize_requested":
					return "episode.summarize_requested";
				case "episode.summarized":
					return "episode.summarized";
				case "memory.user_model.updated":
					return "memory.user_model.updated";
				case "memory.self_model.updated":
					return "memory.self_model.updated";
				case "memory.skill.created":
					return "memory.skill.created";
				case "memory.skill.promoted":
					return "memory.skill.promoted";
				case "memory.node.decayed":
					return "memory.node.decayed";
				case "memory.pattern.synthesized":
					return "memory.pattern.synthesized";
				case "memory.node.merged":
					return "memory.node.merged";
				case "memory.node.importance.propagated":
					return "memory.node.importance.propagated";
				case "memory.node.confidence_adjusted":
					return "memory.node.confidence_adjusted";
				case "memory.node.accessed":
					return "memory.node.accessed";
				// Scheduler
				case "job.created":
					return "job.created";
				case "job.triggered":
					return "job.triggered";
				case "job.completed":
					return "job.completed";
				case "job.failed":
					return "job.failed";
				case "job.cancelled":
					return "job.cancelled";
				case "notification.created":
					return "notification.created";
				// System
				case "system.started":
					return "system.started";
				case "system.stopped":
					return "system.stopped";
				case "system.rollback":
					return "system.rollback";
				case "system.handler.dead_lettered":
					return "system.handler.dead_lettered";
				case "hook.failed":
					return "hook.failed";
				// Goals (Phase 12a)
				case "goal.created":
					return "goal.created";
				case "goal.confirmed":
					return "goal.confirmed";
				case "goal.priority_changed":
					return "goal.priority_changed";
				case "goal.plan_updated":
					return "goal.plan_updated";
				case "goal.lease_acquired":
					return "goal.lease_acquired";
				case "goal.lease_released":
					return "goal.lease_released";
				case "goal.task_started":
					return "goal.task_started";
				case "goal.task_progress":
					return "goal.task_progress";
				case "goal.task_yielded":
					return "goal.task_yielded";
				case "goal.task_completed":
					return "goal.task_completed";
				case "goal.task_failed":
					return "goal.task_failed";
				case "goal.task_abandoned":
					return "goal.task_abandoned";
				case "goal.blocked":
					return "goal.blocked";
				case "goal.unblocked":
					return "goal.unblocked";
				case "goal.reconsidered":
					return "goal.reconsidered";
				case "goal.paused":
					return "goal.paused";
				case "goal.resumed":
					return "goal.resumed";
				case "goal.cancelled":
					return "goal.cancelled";
				case "goal.completed":
					return "goal.completed";
				case "goal.quarantined":
					return "goal.quarantined";
				case "goal.redacted":
					return "goal.redacted";
				case "goal.expired":
					return "goal.expired";
				default:
					return assertNever(event);
			}
		}

		// Runtime check: construct a minimal event and verify switch works
		const event: Event = {
			id: newEventId(),
			type: "turn.completed",
			version: 1,
			timestamp: new Date(),
			actor: "theo",
			data: {
				sessionId: "session-1",
				responseBody: "hello",
				durationMs: 100,
				inputTokens: 30,
				outputTokens: 20,
				totalTokens: 50,
				costUsd: 0.001,
			},
			metadata: {},
		};
		expect(eventLabel(event)).toBe("turn.completed");
	});

	test("EventId branding -- newEventId() returns branded string", () => {
		const id = newEventId();
		// The result is a string at runtime
		expect(typeof id).toBe("string");
		expect(id.length).toBeGreaterThan(0);

		// Compile-time check: the branded type is assignable to EventId
		const assignable: EventId = id;
		expect(assignable).toBe(id);

		// A plain string is NOT assignable to EventId at compile time.
		// We cannot test a compile-time failure at runtime, but we verify
		// that the branding mechanism works by checking the type narrows correctly.
		// The following would fail tsc if uncommented:
		// const plain: EventId = "not-a-ulid";  // Error: Type 'string' is not assignable to type 'EventId'
	});

	test("Readonly enforcement -- event data cannot be mutated", () => {
		// Construct an event with readonly data
		const event: EventOfType<"turn.completed"> = {
			id: newEventId(),
			type: "turn.completed",
			version: 1,
			timestamp: new Date(),
			actor: "theo",
			data: {
				sessionId: "session-1",
				responseBody: "hello",
				durationMs: 100,
				inputTokens: 30,
				outputTokens: 20,
				totalTokens: 50,
				costUsd: 0.001,
			},
			metadata: {},
		};

		// Verify data is accessible
		expect(event.data.responseBody).toBe("hello");
		expect(event.data.durationMs).toBe(100);
		expect(event.data.totalTokens).toBe(50);

		// The following would fail tsc if uncommented (readonly):
		// event.data = { sessionId: "x", responseBody: "bye", durationMs: 0,
		//   inputTokens: 0, outputTokens: 0, totalTokens: 0, costUsd: 0 };
		// event.type = "turn.failed";

		// Runtime assertion: the structure is as expected
		expect(event.type).toBe("turn.completed");
	});

	test("EventOfType extraction -- resolves to correct event variant", () => {
		// EventOfType<"memory.node.updated"> should resolve to
		// TheoEvent<"memory.node.updated", NodeUpdatedData>
		const event: EventOfType<"memory.node.updated"> = {
			id: newEventId(),
			type: "memory.node.updated",
			version: 1,
			timestamp: new Date(),
			actor: "theo",
			data: {
				nodeId: 42,
				update: { field: "body", oldValue: "old", newValue: "new" },
			},
			metadata: {},
		};

		// Type narrowing: data is NodeUpdatedData
		expect(event.data.nodeId).toBe(42);
		expect(event.data.update.field).toBe("body");
	});

	test("EventData extraction -- resolves to correct data payload", () => {
		// EventData<"turn.failed"> should resolve to TurnFailedData
		const data: EventData<"turn.failed"> = {
			sessionId: "session-1",
			errorType: "error_during_execution",
			errors: ["Request timed out"],
			durationMs: 1000,
		};

		// Verify the type resolves correctly at compile time and runtime
		const typed: TurnFailedData = data;
		expect(typed.errorType).toBe("error_during_execution");
		expect(typed.errors).toEqual(["Request timed out"]);
		expect(typed.durationMs).toBe(1000);

		// EventData<"turn.completed"> should resolve to TurnCompletedData
		const completedData: EventData<"turn.completed"> = {
			sessionId: "session-1",
			responseBody: "done",
			durationMs: 250,
			inputTokens: 60,
			outputTokens: 40,
			totalTokens: 100,
			costUsd: 0.002,
		};
		const typedCompleted: TurnCompletedData = completedData;
		expect(typedCompleted.responseBody).toBe("done");
		expect(typedCompleted.totalTokens).toBe(100);
	});

	test("NodeUpdate discriminant -- switching on field narrows oldValue/newValue", () => {
		const updates: NodeUpdate[] = [
			{ field: "body", oldValue: "old text", newValue: "new text" },
			{ field: "kind", oldValue: "fact", newValue: "belief" },
			{ field: "sensitivity", oldValue: "none", newValue: "restricted" },
			{ field: "confidence", oldValue: 0.5, newValue: 0.9 },
		];

		for (const update of updates) {
			switch (update.field) {
				case "body": {
					// Narrowed: oldValue and newValue are strings
					const old: string = update.oldValue;
					const next: string = update.newValue;
					expect(typeof old).toBe("string");
					expect(typeof next).toBe("string");
					break;
				}
				case "kind": {
					// Narrowed: oldValue and newValue are NodeKind
					expect(update.oldValue).toBe("fact");
					expect(update.newValue).toBe("belief");
					break;
				}
				case "sensitivity": {
					// Narrowed: oldValue and newValue are Sensitivity
					expect(update.oldValue).toBe("none");
					expect(update.newValue).toBe("restricted");
					break;
				}
				case "confidence": {
					// Narrowed: oldValue and newValue are numbers
					const old: number = update.oldValue;
					const next: number = update.newValue;
					expect(typeof old).toBe("number");
					expect(typeof next).toBe("number");
					break;
				}
				default:
					assertNever(update);
			}
		}
	});

	test("EphemeralEvent is type-incompatible with Event", () => {
		// Construct an EphemeralEvent
		const ephemeral: EphemeralEvent = {
			type: "stream.chunk",
			data: { text: "hello", sessionId: "s1" },
		};

		// The ephemeral event has a type, but it's NOT in Event["type"]
		expect(ephemeral.type).toBe("stream.chunk");

		// The following would fail tsc if uncommented:
		// const asEvent: Event = ephemeral;  // Error: not assignable

		// Runtime: verify it lacks the fields that Event requires (id, version, etc.)
		expect("id" in ephemeral).toBe(false);
		expect("version" in ephemeral).toBe(false);
		expect("actor" in ephemeral).toBe(false);
	});
});
