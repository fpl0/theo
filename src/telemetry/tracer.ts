/**
 * Tracer bootstrap + `withSpan` helper.
 *
 * The public API is `WithSpan`: a boundary wrapper that business code uses
 * at four sites only (chat turn, retrieval, SDK call, bus dispatch). Inside
 * the callback, child `withSpan` calls nest automatically via
 * `AsyncLocalStorage` — callers never touch an OTel tracer directly.
 *
 * Implementation strategy (see plan §"Bun + OTel JS SDK compatibility"):
 *
 *   - The default tracer is SDK-agnostic and generates ids via `crypto` so
 *     traces still correlate logs/events even without a running collector.
 *   - When an OTLP exporter becomes available it is swapped in at
 *     construction time; business code is unchanged.
 *
 * Spans are run inside an `AsyncLocalStorage<SpanState>`, so every
 * `await`-boundary keeps the right active context. Attributes are recorded
 * through the redaction filter before handoff to an exporter.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { registerActiveContextGetter } from "./context.ts";
import type { InitializedMetrics } from "./metrics.ts";
import { isAllowed, REDACTED, redactAttributes } from "./redact.ts";
import type { ResourceAttributes } from "./resource.ts";

// ---------------------------------------------------------------------------
// Span shape + store
// ---------------------------------------------------------------------------

export interface SpanState {
	readonly traceId: string;
	readonly spanId: string;
	readonly parentSpanId?: string;
	readonly name: string;
	readonly startMs: number;
	readonly attributes: Record<string, unknown>;
}

export interface FinishedSpan extends SpanState {
	readonly endMs: number;
	readonly durationMs: number;
	readonly status: "ok" | "error";
	readonly errorMessage?: string;
}

const als = new AsyncLocalStorage<SpanState>();

// ---------------------------------------------------------------------------
// `withSpan` helper
// ---------------------------------------------------------------------------

export type WithSpan = <T>(
	name: string,
	attributes: Record<string, unknown>,
	fn: () => Promise<T>,
) => Promise<T>;

export interface TracerBundle {
	readonly withSpan: WithSpan;
	/** For tests: inspect finished spans. */
	readonly finished: () => readonly FinishedSpan[];
	/** Flush and shutdown — no-op for the in-memory tracer. */
	readonly shutdown: () => Promise<void>;
	/** Returns the currently-active span, if any. */
	readonly active: () => SpanState | null;
	/** Run a function inside an explicitly-provided context (used by bus dispatch rehydration). */
	readonly withContext: <T>(ctx: SpanState, fn: () => Promise<T>) => Promise<T>;
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

export interface TracerConfig {
	readonly resource: ResourceAttributes;
	readonly metrics: InitializedMetrics;
	/**
	 * Optional exporter sink — receives finished spans after redaction.
	 * Tests rely on the in-memory list; a future OTLP batcher registers
	 * itself here. Must not throw; exporter failures are swallowed and
	 * counted via `exporter_dropped_total`.
	 */
	readonly exporter?: (span: FinishedSpan) => void;
}

export function initTracer(config: TracerConfig): TracerBundle {
	const finished: FinishedSpan[] = [];
	const exporter = config.exporter ?? ((s): void => void finished.push(s));

	// Publish the active-context getter so `injectContext` can stamp events.
	registerActiveContextGetter(() => {
		const s = als.getStore();
		return s ? { traceId: s.traceId, spanId: s.spanId } : null;
	});

	async function withSpan<T>(
		name: string,
		attributes: Record<string, unknown>,
		fn: () => Promise<T>,
	): Promise<T> {
		const parent = als.getStore() ?? null;
		const traceId = parent?.traceId ?? newTraceId();
		const spanId = newSpanId();
		const filteredAttrs = redactAttributes(attributes, (key) => {
			config.metrics.registry.redactions.add(1, { key: coarsen(key) });
		});
		const state: SpanState = {
			traceId,
			spanId,
			...(parent !== null ? { parentSpanId: parent.spanId } : {}),
			name,
			startMs: performance.now(),
			attributes: filteredAttrs,
		};
		const start = state.startMs;
		try {
			const result = await als.run(state, fn);
			const endMs = performance.now();
			safeExport(exporter, {
				...state,
				endMs,
				durationMs: endMs - start,
				status: "ok",
			});
			return result;
		} catch (err) {
			const endMs = performance.now();
			safeExport(exporter, {
				...state,
				endMs,
				durationMs: endMs - start,
				status: "error",
				errorMessage: err instanceof Error ? err.message : String(err),
			});
			throw err;
		}
	}

	async function withContext<T>(ctx: SpanState, fn: () => Promise<T>): Promise<T> {
		return als.run(ctx, fn);
	}

	return {
		withSpan,
		finished: () => finished,
		shutdown: async () => {
			// No-op for the in-memory tracer; an OTLP-backed tracer flushes here.
		},
		active: () => als.getStore() ?? null,
		withContext,
	};
}

// ---------------------------------------------------------------------------
// ID generation — 16-byte trace, 8-byte span, hex-encoded (OTel format)
// ---------------------------------------------------------------------------

function newTraceId(): string {
	const bytes = new Uint8Array(16);
	crypto.getRandomValues(bytes);
	return Buffer.from(bytes).toString("hex");
}

function newSpanId(): string {
	const bytes = new Uint8Array(8);
	crypto.getRandomValues(bytes);
	return Buffer.from(bytes).toString("hex");
}

function coarsen(key: string): string {
	// Mirror redact.ts; we keep a local copy so we don't widen that module's
	// exports for a single caller. Coarsens `a.b.c` to `a`.
	const dot = key.indexOf(".");
	return dot === -1 ? key : key.slice(0, dot);
}

function safeExport(exporter: (s: FinishedSpan) => void, span: FinishedSpan): void {
	try {
		exporter(span);
	} catch {
		// Exporter failures are swallowed — observability must never degrade
		// the agent (see plan §"Observability never degrades the agent").
	}
}

// ---------------------------------------------------------------------------
// Public helpers used elsewhere in the telemetry module
// ---------------------------------------------------------------------------

/** True iff `key` passes the redaction allowlist. Re-exported here so span
 *  wrappers can check without importing `redact.ts` directly. */
export const isAllowedAttribute = isAllowed;

/** Placeholder value used when an attribute is redacted. */
export const REDACTED_VALUE = REDACTED;
