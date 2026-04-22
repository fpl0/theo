/**
 * Async context propagation across bus dispatch.
 *
 * Events are async: a `turn.completed` emitted inside a turn span may be
 * processed by a handler many ms later. Without explicit propagation, the
 * handler's span would be an orphan — dashboards lose the "one trace per
 * goal" shape.
 *
 * The fix: emitted events carry `metadata.traceId` / `spanId` / `traceFlags`.
 * Bus dispatch rehydrates them into the active context before invoking a
 * handler. Replay uses the same path, so a historical turn replays as a
 * coherent trace.
 *
 * This module is SDK-agnostic. The `ActiveContext` interface models the
 * minimum the dispatch layer needs; the tracer implementation supplies the
 * glue.
 */

import type { EventMetadata } from "../events/types.ts";

/** Active trace/span context — what business code carries via `AsyncLocalStorage`. */
export interface ActiveContext {
	readonly traceId: string;
	readonly spanId: string;
	readonly traceFlags?: number;
}

/** Hook-point: the tracer registers a getter so `injectContext` can read the active context. */
type ActiveContextGetter = () => ActiveContext | null;
let activeGetter: ActiveContextGetter = () => null;

/** Called once by the tracer bootstrap. */
export function registerActiveContextGetter(getter: ActiveContextGetter): void {
	activeGetter = getter;
}

/**
 * Annotate `metadata` with the current trace/span ids. No-op when no
 * tracer is registered (the no-op tracer case). Pure — returns a new
 * metadata object; the input is not mutated.
 */
export function injectContext(metadata: EventMetadata): EventMetadata {
	if (metadata.traceId !== undefined) return metadata;
	const active = activeGetter();
	if (active === null) return metadata;
	// EventMetadata does not currently reserve a `spanId` field. Rather than
	// bleed observability schema into the base event shape, we pack the trace
	// id into the existing `traceId` field and leave `spanId` in the metadata
	// for handler-side rehydration. The bus / storage layer treats unknown
	// metadata keys as opaque.
	const out: EventMetadata & { readonly spanId?: string; readonly traceFlags?: number } = {
		...metadata,
		traceId: active.traceId,
		spanId: active.spanId,
		...(active.traceFlags !== undefined ? { traceFlags: active.traceFlags } : {}),
	};
	return out;
}

/**
 * Pull the trace context off an event's metadata so the handler can run
 * under it. Returns `null` when the event carries no context — handlers
 * then execute as a fresh root.
 */
export function rehydrateContext(metadata: EventMetadata): ActiveContext | null {
	const m = metadata as EventMetadata & { readonly spanId?: string; readonly traceFlags?: number };
	if (m.traceId === undefined || m.spanId === undefined) return null;
	return {
		traceId: m.traceId,
		spanId: m.spanId,
		...(m.traceFlags !== undefined ? { traceFlags: m.traceFlags } : {}),
	};
}
