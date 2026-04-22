/**
 * Webhook secret store — rotatable, never logged, never in events.
 *
 * Key material lives only in the `webhook_secret` table. Rotation uses a
 * 7-day grace window during which verification accepts either the current
 * or the previous key. After expiry `secret_previous` is cleared and a
 * `webhook.secret_grace_expired` event is emitted.
 *
 * Every mutation goes through this module so the biome rule against
 * interpolating secret columns stays enforceable. Consumers receive a
 * `SecretPair`; they never see the raw column name.
 */

import { randomBytes } from "node:crypto";
import type { Sql } from "postgres";
import { asQueryable } from "../../db/pool.ts";
import type { EventBus } from "../../events/bus.ts";
import type { Actor } from "../../events/types.ts";
import type { SecretPair } from "./signature.ts";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Rotation grace window — seven days. Tests pass a shorter value via options. */
const DEFAULT_GRACE_MS = 7 * 24 * 60 * 60_000;

/** Length of newly generated secrets, in bytes. Hex-encoded in the DB. */
const SECRET_BYTE_LENGTH = 32;

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

export interface WebhookSecretStore {
	/** Return the active secret pair for a source, or null when no secret is registered. */
	getSecrets(source: string): Promise<SecretPair | null>;

	/** Register a new source with a freshly generated secret. Returns the plain secret once. */
	register(source: string, actor: Actor): Promise<string>;

	/** Rotate the secret for a source. Emits `webhook.secret_rotated`. */
	rotate(source: string, actor: Actor): Promise<string>;

	/** Sweep expired previous secrets. Emits `webhook.secret_grace_expired` for each. */
	sweepExpired(at: Date): Promise<readonly string[]>;
}

export interface WebhookSecretStoreOptions {
	readonly graceMs?: number;
	readonly now?: () => Date;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export function createWebhookSecretStore(
	sql: Sql,
	bus: EventBus,
	options: WebhookSecretStoreOptions = {},
): WebhookSecretStore {
	const graceMs = options.graceMs ?? DEFAULT_GRACE_MS;
	const now = options.now ?? ((): Date => new Date());

	/**
	 * Generate a fresh hex-encoded secret. 32 bytes → 64 hex chars; enough
	 * entropy for HMAC-SHA256 with a wide safety margin.
	 */
	function newSecret(): string {
		return randomBytes(SECRET_BYTE_LENGTH).toString("hex");
	}

	async function getSecrets(source: string): Promise<SecretPair | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT secret_current, secret_previous, secret_previous_expires_at
			FROM webhook_secret
			WHERE source = ${source}
		`;
		const row = rows[0];
		if (!row) return null;
		// If grace window has passed, pretend there is no previous secret.
		const currentTime = now().getTime();
		const prev = row["secret_previous"] as string | null;
		const prevExpires = row["secret_previous_expires_at"] as Date | null;
		const previousActive =
			prev !== null && prevExpires !== null && prevExpires.getTime() > currentTime;
		return {
			current: row["secret_current"] as string,
			previous: previousActive ? prev : null,
		};
	}

	async function register(source: string, actor: Actor): Promise<string> {
		const secret = newSecret();
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			await q`
				INSERT INTO webhook_secret (source, secret_current, rotated_at)
				VALUES (${source}, ${secret}, now())
				ON CONFLICT (source) DO NOTHING
			`;
			await bus.emit(
				{
					type: "webhook.secret_rotated",
					version: 1,
					actor,
					data: { source, rotatedBy: actor },
					metadata: {},
				},
				{ tx },
			);
		});
		return secret;
	}

	async function rotate(source: string, actor: Actor): Promise<string> {
		const fresh = newSecret();
		const expiresAt = new Date(now().getTime() + graceMs);
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Record<string, unknown>[]>`
				SELECT secret_current FROM webhook_secret WHERE source = ${source}
			`;
			const row = rows[0];
			if (!row) {
				await q`
					INSERT INTO webhook_secret (source, secret_current, rotated_at)
					VALUES (${source}, ${fresh}, now())
				`;
			} else {
				await q`
					UPDATE webhook_secret
					SET secret_previous = ${row["secret_current"] as string},
					    secret_previous_expires_at = ${expiresAt},
					    secret_current = ${fresh},
					    rotated_at = now()
					WHERE source = ${source}
				`;
			}
			await bus.emit(
				{
					type: "webhook.secret_rotated",
					version: 1,
					actor,
					data: { source, rotatedBy: actor },
					metadata: {},
				},
				{ tx },
			);
		});
		return fresh;
	}

	async function sweepExpired(at: Date): Promise<readonly string[]> {
		const affected: string[] = [];
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<{ source: string }[]>`
				SELECT source FROM webhook_secret
				WHERE secret_previous IS NOT NULL
				  AND secret_previous_expires_at IS NOT NULL
				  AND secret_previous_expires_at <= ${at}
			`;
			for (const row of rows) {
				affected.push(row.source);
			}
			if (affected.length === 0) return;
			await q`
				UPDATE webhook_secret
				SET secret_previous = NULL,
				    secret_previous_expires_at = NULL
				WHERE source = ANY(${affected}::text[])
			`;
			for (const source of affected) {
				await bus.emit(
					{
						type: "webhook.secret_grace_expired",
						version: 1,
						actor: "system",
						data: { source },
						metadata: {},
					},
					{ tx },
				);
			}
		});
		return affected;
	}

	return { getSecrets, register, rotate, sweepExpired };
}
