/**
 * Per-source token-bucket rate limiter.
 *
 * Default policy: 60 requests per minute, burst capacity 10 tokens. Tokens
 * refill at `refill_rate` per second up to `capacity`. One request consumes
 * one token. When the bucket is empty, the request is throttled and the
 * caller returns HTTP 429 plus a `Retry-After` header.
 *
 * The bucket is persisted in `reflex_rate_limit`. Under load it takes one
 * UPDATE per request; a partial index could further tighten this, but the
 * row-count is small (one per source) so the write is O(1) and fine for
 * the expected per-second rate of webhooks (single-digit).
 */

import type { Sql } from "postgres";
import { asQueryable } from "../../db/pool.ts";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

export const DEFAULT_CAPACITY = 10;
export const DEFAULT_REFILL_RATE = 1; // 60/min = 1/s

// ---------------------------------------------------------------------------
// Outcome
// ---------------------------------------------------------------------------

export type RateLimitDecision =
	| { readonly allowed: true; readonly remaining: number }
	| { readonly allowed: false; readonly retryAfterSec: number };

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface RateLimitPolicy {
	readonly capacity: number;
	readonly refillRate: number;
}

export interface RateLimiterOptions {
	readonly now?: () => Date;
	readonly defaultPolicy?: RateLimitPolicy;
}

// ---------------------------------------------------------------------------
// Limiter
// ---------------------------------------------------------------------------

export interface RateLimiter {
	/** Consume one token for `source`. Returns whether the request is allowed. */
	consume(source: string, policy?: RateLimitPolicy): Promise<RateLimitDecision>;

	/** Reset the bucket for tests / operator commands. */
	reset(source: string): Promise<void>;
}

export function createRateLimiter(sql: Sql, options: RateLimiterOptions = {}): RateLimiter {
	const now = options.now ?? ((): Date => new Date());
	const defaultPolicy: RateLimitPolicy = options.defaultPolicy ?? {
		capacity: DEFAULT_CAPACITY,
		refillRate: DEFAULT_REFILL_RATE,
	};

	async function consume(
		source: string,
		policy: RateLimitPolicy = defaultPolicy,
	): Promise<RateLimitDecision> {
		const currentTime = now();
		return sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Record<string, unknown>[]>`
				SELECT tokens, last_refill, capacity, refill_rate
				FROM reflex_rate_limit
				WHERE source = ${source}
				FOR UPDATE
			`;
			const row = rows[0];
			if (!row) {
				// First request from this source — seed with capacity-1 tokens and allow.
				await q`
					INSERT INTO reflex_rate_limit (source, tokens, last_refill, capacity, refill_rate)
					VALUES (${source}, ${policy.capacity - 1}, ${currentTime},
					        ${policy.capacity}, ${policy.refillRate})
				`;
				return {
					allowed: true,
					remaining: policy.capacity - 1,
				};
			}

			// Refill based on elapsed time since last_refill.
			const tokens = row["tokens"] as number;
			const capacity = row["capacity"] as number;
			const refillRate = row["refill_rate"] as number;
			const lastRefill = row["last_refill"] as Date;
			const elapsedSec = Math.max(0, (currentTime.getTime() - lastRefill.getTime()) / 1000);
			const refilled = Math.min(capacity, tokens + elapsedSec * refillRate);

			if (refilled < 1) {
				// Not enough — compute Retry-After.
				const needed = 1 - refilled;
				const retryAfterSec = Math.max(1, Math.ceil(needed / refillRate));
				// Persist the refill without consuming a token so the next call
				// sees the accrued value.
				await q`
					UPDATE reflex_rate_limit
					SET tokens = ${refilled}, last_refill = ${currentTime}
					WHERE source = ${source}
				`;
				return { allowed: false, retryAfterSec };
			}

			const remaining = refilled - 1;
			await q`
				UPDATE reflex_rate_limit
				SET tokens = ${remaining}, last_refill = ${currentTime}
				WHERE source = ${source}
			`;
			return { allowed: true, remaining };
		});
	}

	async function reset(source: string): Promise<void> {
		await sql`
			DELETE FROM reflex_rate_limit WHERE source = ${source}
		`;
	}

	return { consume, reset };
}
