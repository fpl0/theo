/**
 * Webhook gate — the only unauthenticated write path into Theo.
 *
 * Defense in depth (see `docs/plans/foundation/13b-ideation-and-reflexes.md`
 * §2):
 *
 *   1. Network: loopback-only by default. Public exposure requires a tunnel.
 *   2. Body size: 1 MB cap. Oversize returns 413.
 *   3. Dedup: `(source, delivery_id)` unique insert. Duplicates return 200.
 *   4. HMAC: `timingSafeEqual` against current + (if grace) previous secret.
 *   5. Rate limit: token bucket per source. Excess returns 429.
 *   6. Staleness: > `maxStaleMs` is recorded but not reflex-triggered.
 *   7. Source allowlist: only configured sources accept.
 *
 * This module is parser + state glue. Signature verification lives in
 * `signature.ts`, secret storage in `secrets.ts`, rate limiting in
 * `rate_limit.ts`, envelope wrapping in `envelope.ts`. The server itself
 * does not invoke the LLM — it emits `webhook.received` → `webhook.verified`
 * and lets the reflex decision handler take over from there.
 */

import type { Sql, TransactionSql } from "postgres";
import { monotonicFactory } from "ulid";
import { asQueryable } from "../../db/pool.ts";
import { describeError } from "../../errors.ts";
import type { EventBus } from "../../events/bus.ts";
import type { RateLimiter } from "./rate_limit.ts";
import type { WebhookSecretStore } from "./secrets.ts";
import { type KnownSource, verifierFor } from "./signature.ts";
import { parseEmailPayload } from "./sources/email.ts";
import { parseGithubPayload } from "./sources/github.ts";
import { parseLinearPayload } from "./sources/linear.ts";
import type { Parser } from "./sources/types.ts";

const ulid = monotonicFactory();

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maximum accepted body size — 1 MB. Requests larger get 413. */
export const MAX_BODY_BYTES = 1_000_000;

/** Default staleness window — events older than this are recorded but not reflex-triggered. */
export const DEFAULT_MAX_STALE_MS = 60 * 60_000;

/** Transient webhook body rows expire after 24 h. */
export const WEBHOOK_BODY_TTL_MS = 24 * 60 * 60_000;

// ---------------------------------------------------------------------------
// Server config
// ---------------------------------------------------------------------------

export type WebhookTunnel = "cloudflare" | "tailscale" | "relay";

export interface WebhookServerConfig {
	/** TCP port — default 4911. */
	readonly port: number;
	/** Bind hostname — loopback-only unless public is explicitly enabled. */
	readonly hostname?: string;
	/** Opt-in to public binding. Requires a tunnel adapter. */
	readonly public?: boolean;
	readonly tunnel?: WebhookTunnel;
	/** Sources that the gate is allowed to accept. Unknown sources return 404. */
	readonly sources: readonly KnownSource[];
	/** Staleness window, ms. */
	readonly maxStaleMs?: number;
}

export interface WebhookServerDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly secrets: WebhookSecretStore;
	readonly rateLimiter: RateLimiter;
	readonly config: WebhookServerConfig;
	readonly now?: () => Date;
}

// ---------------------------------------------------------------------------
// Parsers
// ---------------------------------------------------------------------------

const SOURCE_PARSERS: Readonly<Record<KnownSource, Parser>> = {
	github: parseGithubPayload,
	linear: parseLinearPayload,
	email: parseEmailPayload,
};

function signatureHeader(
	source: KnownSource,
	headers: Record<string, string | undefined>,
): string | null {
	switch (source) {
		case "github":
			return headers["x-hub-signature-256"] ?? null;
		case "linear":
			return headers["linear-signature"] ?? null;
		case "email":
			return headers["x-theo-relay-signature"] ?? null;
	}
}

function collectHeaders(request: Request): Record<string, string> {
	const out: Record<string, string> = {};
	request.headers.forEach((value, key) => {
		out[key.toLowerCase()] = value;
	});
	return out;
}

// ---------------------------------------------------------------------------
// Body hashing
// ---------------------------------------------------------------------------

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
	const digest = await crypto.subtle.digest("SHA-256", bytes);
	const view = new Uint8Array(digest);
	let out = "";
	for (const byte of view) {
		out += byte.toString(16).padStart(2, "0");
	}
	return out;
}

// ---------------------------------------------------------------------------
// Route handling
// ---------------------------------------------------------------------------

interface RouteResult {
	readonly status: number;
	readonly body?: string;
	readonly headers?: Record<string, string>;
}

/**
 * Handle one webhook request end to end. Returns the HTTP response shape;
 * the caller (Bun.serve) translates it to a Response.
 */
export async function handleWebhookRequest(
	deps: WebhookServerDeps,
	source: string,
	request: Request,
): Promise<RouteResult> {
	const { sql, bus, secrets, rateLimiter, config } = deps;

	// 1. Source allowlist.
	if (!config.sources.includes(source as KnownSource)) {
		await emitRejection(bus, source, null, "unknown_source");
		return { status: 404, body: "unknown source" };
	}
	const parser = SOURCE_PARSERS[source as KnownSource];

	// 2. Method + length checks. Bun's Request carries the body Stream; we
	//    read it once as a Buffer to cap and hash in one pass.
	if (request.method !== "POST") {
		return { status: 405, body: "method not allowed" };
	}
	const lenHeader = request.headers.get("content-length");
	if (lenHeader !== null) {
		const len = Number.parseInt(lenHeader, 10);
		if (Number.isFinite(len) && len > MAX_BODY_BYTES) {
			await emitRejection(bus, source, null, "body_too_large");
			return { status: 413, body: "body too large" };
		}
	}

	const rawBytes = await request.arrayBuffer();
	if (rawBytes.byteLength > MAX_BODY_BYTES) {
		await emitRejection(bus, source, null, "body_too_large");
		return { status: 413, body: "body too large" };
	}
	const bodyBuffer = Buffer.from(rawBytes);

	// 3. JSON parse — never embed parse-failure content in logs.
	let parsed: unknown;
	try {
		parsed = JSON.parse(bodyBuffer.toString("utf8"));
	} catch {
		await emitRejection(bus, source, null, "parse_error");
		return { status: 400, body: "parse error" };
	}

	const headers = collectHeaders(request);
	const headerUndefined: Record<string, string | undefined> = { ...headers };
	const parsedWebhook = parser(parsed, headerUndefined);
	if (!parsedWebhook) {
		await emitRejection(bus, source, null, "parse_error");
		return { status: 400, body: "parse error" };
	}

	// 4. Rate limit.
	const rateDecision = await rateLimiter.consume(source);
	if (!rateDecision.allowed) {
		await bus.emit({
			type: "webhook.rate_limited",
			version: 1,
			actor: "system",
			data: { source, retryAfterSec: rateDecision.retryAfterSec },
			metadata: {},
		});
		await recordDelivery(sql, source, parsedWebhook.deliveryId, false, "rate_limited");
		return {
			status: 429,
			body: "rate limited",
			headers: { "retry-after": String(rateDecision.retryAfterSec) },
		};
	}

	// 5. Dedup — (source, delivery_id) unique insert. First writer wins.
	const alreadySeen = await isDuplicate(sql, source, parsedWebhook.deliveryId);
	if (alreadySeen) {
		await emitRejection(bus, source, parsedWebhook.deliveryId, "duplicate");
		return { status: 200, body: "duplicate" };
	}

	// 6. HMAC.
	const secretPair = await secrets.getSecrets(source);
	if (!secretPair) {
		await emitRejection(bus, source, parsedWebhook.deliveryId, "unknown_source");
		return { status: 404, body: "unknown source" };
	}
	const sigHeader = signatureHeader(source as KnownSource, headerUndefined);
	const verifier = verifierFor(source);
	const signatureOk =
		sigHeader !== null && verifier !== null ? verifier(bodyBuffer, sigHeader, secretPair) : false;
	if (!signatureOk) {
		await recordDelivery(sql, source, parsedWebhook.deliveryId, false, "rejected");
		await emitRejection(bus, source, parsedWebhook.deliveryId, "signature_invalid");
		return { status: 401, body: "signature invalid" };
	}

	// 7. Hash + byte length for the durable `webhook.received` event.
	const bodyHash = await sha256Hex(rawBytes);
	const receivedAt = (deps.now ?? ((): Date => new Date()))();

	// 8. Store the raw body in the transient table, then emit the two
	//    decision events in sequence. The dedup row is written last so a
	//    duplicate second attempt doesn't re-emit.
	const payloadRef = ulid();
	await sql.begin(async (tx) => {
		const q = asQueryable(tx);
		await q`
			INSERT INTO webhook_body (id, source, body, expires_at)
			VALUES (${payloadRef}, ${source}, ${sql.json(parsed as never)},
			        ${new Date(receivedAt.getTime() + WEBHOOK_BODY_TTL_MS)})
		`;

		const receivedEvent = await bus.emit(
			{
				type: "webhook.received",
				version: 1,
				actor: "system",
				data: {
					source,
					deliveryId: parsedWebhook.deliveryId,
					bodyHash,
					bodyByteLength: rawBytes.byteLength,
					receivedAt: receivedAt.toISOString(),
				},
				metadata: {},
			},
			{ tx, seedTier: "external" },
		);

		await bus.emit(
			{
				type: "webhook.verified",
				version: 1,
				actor: "system",
				data: {
					source,
					deliveryId: parsedWebhook.deliveryId,
					payloadRef,
				},
				metadata: { causeId: receivedEvent.id },
			},
			{ tx, seedTier: "external" },
		);

		await recordDelivery(tx, source, parsedWebhook.deliveryId, true, "accepted");
	});

	return { status: 200, body: "ok" };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function emitRejection(
	bus: EventBus,
	source: string,
	deliveryId: string | null,
	reason:
		| "parse_error"
		| "signature_invalid"
		| "body_too_large"
		| "unknown_source"
		| "duplicate"
		| "stale",
): Promise<void> {
	await bus.emit({
		type: "webhook.rejected",
		version: 1,
		actor: "system",
		data: { source, deliveryId, reason },
		metadata: {},
	});
}

async function isDuplicate(sql: Sql, source: string, deliveryId: string): Promise<boolean> {
	const rows = await sql<{ source: string }[]>`
		SELECT source FROM webhook_delivery WHERE source = ${source} AND delivery_id = ${deliveryId}
	`;
	return rows.length > 0;
}

async function recordDelivery(
	sql: Sql | TransactionSql,
	source: string,
	deliveryId: string,
	signatureOk: boolean,
	outcome: "accepted" | "rejected" | "rate_limited" | "stale" | "duplicate",
): Promise<void> {
	const q = asQueryable(sql);
	await q`
		INSERT INTO webhook_delivery (source, delivery_id, signature_ok, outcome)
		VALUES (${source}, ${deliveryId}, ${signatureOk}, ${outcome})
		ON CONFLICT (source, delivery_id) DO NOTHING
	`;
}

// ---------------------------------------------------------------------------
// Bun.serve integration
// ---------------------------------------------------------------------------

/**
 * Start the webhook server. Returns a `Server` handle the engine can stop.
 *
 * The server binds to `127.0.0.1` by default. `config.public = true`
 * requires `config.tunnel` to be set; a bare public bind is refused.
 */
export function startWebhookServer(deps: WebhookServerDeps): ReturnType<typeof Bun.serve> {
	const { config } = deps;
	if (config.public === true && config.tunnel === undefined) {
		throw new Error(
			"webhook server refuses to bind publicly without a tunnel — " +
				"set config.tunnel to 'cloudflare' | 'tailscale' | 'relay' first",
		);
	}
	const hostname = config.public === true ? (config.hostname ?? "0.0.0.0") : "127.0.0.1";

	return Bun.serve({
		port: config.port,
		hostname,
		async fetch(request: Request) {
			const url = new URL(request.url);
			const pathParts = url.pathname.split("/").filter((p) => p.length > 0);
			const source = pathParts[0];
			if (source === undefined) {
				return new Response("not found", { status: 404 });
			}
			try {
				const result = await handleWebhookRequest(deps, source, request);
				const init: ResponseInit =
					result.headers !== undefined
						? { status: result.status, headers: result.headers }
						: { status: result.status };
				return new Response(result.body ?? "", init);
			} catch (error) {
				console.error(`webhook gate failed: ${describeError(error)}`);
				return new Response("internal error", { status: 500 });
			}
		},
	});
}
