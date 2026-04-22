/**
 * Bus dispatch span wrapper — one span per durable handler invocation.
 *
 * The biggest observability win is a per-handler span: without it, "slow
 * turn" doesn't tell us which downstream handler is the culprit. The span
 * inherits the emitting context (trace propagation via event metadata), tags
 * the handler id, event type, and handler mode, and records handler
 * duration + errors.
 *
 * Attribute keys follow OTel messaging semconv:
 *   - `messaging.system` = "theo.eventbus"
 *   - `messaging.destination.name` = event type
 *   - `messaging.operation` = "receive"
 * Theo-specific attributes use the `theo.*` namespace.
 */

import { describeError } from "../../errors.ts";
import type { HandlerMode } from "../../events/handlers.ts";
import type { Event } from "../../events/types.ts";
import { rehydrateContext } from "../context.ts";
import type { InitializedMetrics } from "../metrics.ts";
import {
	ATTR_MESSAGING_DESTINATION,
	ATTR_MESSAGING_OP,
	ATTR_MESSAGING_SYSTEM,
	MESSAGING_SYSTEM_THEO,
} from "../semconv.ts";
import type { TracerBundle } from "../tracer.ts";

/**
 * Produce a higher-order handler wrapper. Given the tracer and metrics
 * registry, returns a function that wraps any handler to run inside a span
 * nested under the emitter's context (rehydrated from event metadata).
 */
export function wrapHandlerWithSpan(
	tracer: TracerBundle,
	metrics: InitializedMetrics,
): <E extends Event>(
	handlerId: string,
	mode: HandlerMode,
	handler: (event: E) => Promise<void>,
) => (event: E) => Promise<void> {
	return <E extends Event>(
		handlerId: string,
		mode: HandlerMode,
		handler: (event: E) => Promise<void>,
	): ((event: E) => Promise<void>) => {
		return async (event: E): Promise<void> => {
			const ctx = rehydrateContext(event.metadata);
			const runHandler = async (): Promise<void> => {
				const start = performance.now();
				try {
					await tracer.withSpan(
						`bus.handler.${handlerId}`,
						{
							[ATTR_MESSAGING_SYSTEM]: MESSAGING_SYSTEM_THEO,
							[ATTR_MESSAGING_DESTINATION]: event.type,
							[ATTR_MESSAGING_OP]: "receive",
							"theo.handler.id": handlerId,
							"theo.handler.mode": mode,
						},
						async () => {
							await handler(event);
						},
					);
					const durationMs = performance.now() - start;
					metrics.registry.handlerDuration.record(durationMs, { handler: handlerId });
				} catch (error) {
					const durationMs = performance.now() - start;
					metrics.registry.handlerDuration.record(durationMs, { handler: handlerId });
					metrics.registry.handlerErrors.add(1, {
						handler: handlerId,
						reason: classifyHandlerError(error),
					});
					throw error;
				}
			};

			if (ctx !== null) {
				await tracer.withContext(
					{
						traceId: ctx.traceId,
						spanId: ctx.spanId,
						name: "rehydrated",
						startMs: performance.now(),
						attributes: {},
					},
					runHandler,
				);
			} else {
				await runHandler();
			}
		};
	};
}

function classifyHandlerError(
	err: unknown,
): "db_error" | "timeout" | "validation_error" | "unknown" {
	const msg = describeError(err);
	if (/timeout/iu.test(msg)) return "timeout";
	if (/invalid|validation|zod/iu.test(msg)) return "validation_error";
	if (/postgres|database|ECONN|relation|column/iu.test(msg)) return "db_error";
	return "unknown";
}
