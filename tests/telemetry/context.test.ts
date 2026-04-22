/**
 * Context propagation — `withSpan` nests child spans under the active
 * parent, `injectContext` stamps events with the active trace ids, and
 * `rehydrateContext` extracts them back out.
 */

import { describe, expect, test } from "bun:test";
import {
	injectContext,
	registerActiveContextGetter,
	rehydrateContext,
} from "../../src/telemetry/context.ts";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { buildResource } from "../../src/telemetry/resource.ts";
import { initTracer } from "../../src/telemetry/tracer.ts";

// Low-entropy trace/span ids — valid hex, but the all-ascending pattern keeps
// the `noSecrets` heuristic quiet. Length still matches OTel (32 hex trace id,
// 16 hex span id).
const TRACE_ID = "aaaaaaaabbbbbbbbccccccccdddddddd";
const SPAN_ID = "eeeeeeeeffffffff";

describe("span/event context", () => {
	test("nested withSpan shares traceId, distinct spanIds, parent-child link", async () => {
		const resource = await buildResource({ environment: "test", gitSha: "test", instanceId: "h" });
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource, metrics });
		let innerTrace = "";
		let innerSpan = "";
		let outerSpan = "";
		await tracer.withSpan("outer", {}, async () => {
			const outer = tracer.active();
			if (outer === null) throw new Error("expected outer span active");
			outerSpan = outer.spanId;
			await tracer.withSpan("inner", {}, async () => {
				const inner = tracer.active();
				if (inner === null) throw new Error("expected inner span active");
				innerTrace = inner.traceId;
				innerSpan = inner.spanId;
				expect(inner.parentSpanId).toBe(outerSpan);
			});
		});
		expect(innerTrace).toHaveLength(32);
		expect(innerSpan).toHaveLength(16);
		expect(innerSpan).not.toBe(outerSpan);
	});

	test("injectContext stamps active trace ids onto event metadata", async () => {
		const resource = await buildResource({ environment: "test", gitSha: "test", instanceId: "h" });
		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource, metrics });
		await tracer.withSpan("emitter", {}, async () => {
			const md = injectContext({});
			const active = tracer.active();
			if (active === null) throw new Error("expected active span");
			expect(md.traceId).toBe(active.traceId);
			expect((md as { readonly spanId?: string }).spanId).toBe(active.spanId);
		});
	});

	test("rehydrateContext recovers ids from metadata", () => {
		const ctx = rehydrateContext({
			traceId: TRACE_ID,
			// spanId is stored as a non-standard metadata key; rehydrate reads it.
			...({ spanId: SPAN_ID } as Record<string, unknown>),
		} as never);
		expect(ctx?.traceId).toBe(TRACE_ID);
		expect(ctx?.spanId).toBe(SPAN_ID);
	});

	test("no active context → injectContext returns metadata unchanged", () => {
		registerActiveContextGetter(() => null);
		const md = injectContext({ gate: "cli.owner" });
		expect(md).toEqual({ gate: "cli.owner" });
	});
});
