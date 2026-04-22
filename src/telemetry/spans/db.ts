/**
 * Postgres query-timing hook.
 *
 * Wraps a postgres.js `sql` tagged template in a proxy that times every
 * query and records to `theo.db.query_duration_ms`. Attributes follow OTel
 * db semconv:
 *
 *   - `db.system` = "postgresql"
 *   - `db.operation` = derived from statement (SELECT/INSERT/...)
 *   - `db.statement` = coarsened via `redact.coarsenDbStatement`
 *
 * The wrapper is transparent: all other behaviors of `sql` (transactions,
 * array helpers, unsafe queries, etc.) pass through. We intercept ONLY the
 * tagged-template call signature.
 *
 * Per the plan's resilience principle, timing failures never propagate to
 * the query path — if the metric sink throws, the query still runs.
 */

import type { Sql } from "postgres";
import type { InitializedMetrics } from "../metrics.ts";
import { coarsenDbStatement } from "../redact.ts";

/**
 * Wrap a postgres.js `sql` object so every tagged-template query is timed.
 *
 * `Sql` is a callable with many non-call properties (`sql.begin`,
 * `sql.unsafe`, etc.). We return a Proxy that forwards property access to
 * the original and intercepts the `apply` trap for the tagged-template
 * invocation.
 */
export function instrumentSql(sql: Sql, metrics: InitializedMetrics): Sql {
	const callable = sql as unknown as (
		strings: TemplateStringsArray,
		...values: readonly unknown[]
	) => Promise<unknown> & { then: unknown };

	const handler: ProxyHandler<typeof callable> = {
		apply(target, thisArg, argArray): unknown {
			// postgres.js itself sometimes invokes `sql(connection)` internally.
			// Only tagged-template calls (TemplateStringsArray as first arg)
			// get timed.
			const first = argArray[0] as unknown;
			if (!isTemplateStringsArray(first)) {
				return Reflect.apply(target, thisArg, argArray) as unknown;
			}
			const strings = first;
			const statement = strings.join("?");
			const started = performance.now();
			const result = Reflect.apply(target, thisArg, argArray) as Promise<unknown>;

			// Fire-and-forget timing. `.then` + `.catch` both record; we attach
			// via `Promise.prototype.then` on the returned PendingQuery but NOT
			// on the chain the caller uses — postgres.js PendingQuery is a
			// thenable with one-shot resolution; attaching here is safe.
			result
				.then(
					() => recordDuration(metrics, started, statement, true),
					() => recordDuration(metrics, started, statement, false),
				)
				.catch(() => {
					// Swallow — already recorded; the caller's promise chain has
					// the real error.
				});

			return result as unknown;
		},
		get(target, prop, receiver): unknown {
			return Reflect.get(target, prop, receiver);
		},
	};

	return new Proxy(callable, handler) as unknown as Sql;
}

function isTemplateStringsArray(value: unknown): value is TemplateStringsArray {
	return Array.isArray(value) && Array.isArray((value as unknown as { raw?: unknown }).raw);
}

function recordDuration(
	metrics: InitializedMetrics,
	startedMs: number,
	statement: string,
	ok: boolean,
): void {
	try {
		const durationMs = performance.now() - startedMs;
		const operation = extractOperation(statement);
		metrics.registry.dbQueryDuration.record(durationMs, {
			operation,
			status: ok ? "ok" : "failed",
			table: extractTable(statement),
		});
	} catch {
		// Timing failures never propagate.
	}
}

function extractOperation(statement: string): string {
	const match =
		/^\s*(SELECT|INSERT|UPDATE|DELETE|UPSERT|WITH|BEGIN|COMMIT|ROLLBACK|TRUNCATE|CREATE|DROP|ALTER)\b/iu.exec(
			statement,
		);
	return match?.[1]?.toUpperCase() ?? "UNKNOWN";
}

function extractTable(statement: string): string {
	// Same heuristic as `coarsenDbStatement` — keeps the reported table short
	// and avoids carrying user content into the metric label.
	const coarse = coarsenDbStatement(statement);
	const match = /FROM\s+([a-zA-Z_][\w.]*)/u.exec(coarse);
	return match?.[1] ?? "unknown";
}
