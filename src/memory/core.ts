/**
 * CoreMemoryRepository: the agent's working RAM.
 *
 * Four named JSON documents (persona, goals, user_model, context) are loaded
 * into the system prompt on every turn. Core memory is always present and
 * never truncated — it is Theo's persistent identity.
 *
 * Every update records a changelog entry (before/after) and emits a
 * `memory.core.updated` event, all within a single transaction. The hash()
 * method computes a deterministic hash of all slots so the session manager
 * can detect when the system prompt needs refreshing.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import { err, ok, type Result } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import type { Actor } from "../events/types.ts";
import type { CoreMemory, CoreMemorySlot, JsonValue } from "./types.ts";
import { SlotNotFoundError } from "./types.ts";

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export class CoreMemoryRepository {
	constructor(
		private readonly sql: Sql,
		private readonly bus: EventBus,
	) {}

	/**
	 * Read all 4 core memory slots. Returns a CoreMemory object with one
	 * field per slot. Slots are seeded by migration — if fewer than 4 exist,
	 * something is wrong, but we handle it defensively.
	 */
	async read(): Promise<CoreMemory> {
		const rows = await this.sql`SELECT slot, body FROM core_memory ORDER BY slot`;

		const slotMap = new Map<string, JsonValue>();
		for (const row of rows) {
			const r = row as Record<string, unknown>;
			slotMap.set(r["slot"] as string, r["body"] as JsonValue);
		}

		return {
			persona: slotMap.get("persona") ?? {},
			goals: slotMap.get("goals") ?? {},
			userModel: slotMap.get("user_model") ?? {},
			context: slotMap.get("context") ?? {},
		};
	}

	/**
	 * Read a single slot. Returns Result to handle the (unlikely but possible)
	 * case where a slot has been deleted from the database.
	 */
	async readSlot(slot: CoreMemorySlot): Promise<Result<JsonValue, SlotNotFoundError>> {
		const rows = await this.sql`SELECT body FROM core_memory WHERE slot = ${slot}`;
		const row = rows[0];
		if (row === undefined) {
			return err(new SlotNotFoundError(slot));
		}
		return ok((row as Record<string, unknown>)["body"] as JsonValue);
	}

	/**
	 * Update a slot's body. Records a changelog entry with before/after values
	 * and emits a `memory.core.updated` event — all in a single transaction.
	 */
	async update(slot: CoreMemorySlot, newBody: JsonValue, actor: Actor): Promise<void> {
		await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);

			const currentRows = await q`SELECT body FROM core_memory WHERE slot = ${slot} FOR UPDATE`;
			const currentRow = currentRows[0];
			if (currentRow === undefined) {
				throw new SlotNotFoundError(slot);
			}
			const currentBody = (currentRow as Record<string, unknown>)["body"] as JsonValue;

			// No-op: skip write if value is unchanged
			if (JSON.stringify(currentBody) === JSON.stringify(newBody)) return;

			await q`UPDATE core_memory SET body = ${this.sql.json(newBody)} WHERE slot = ${slot}`;

			await q`
				INSERT INTO core_memory_changelog (slot, body_before, body_after, changed_by)
				VALUES (${slot}, ${this.sql.json(currentBody)}, ${this.sql.json(newBody)}, ${actor})
			`;

			await this.bus.emit(
				{
					type: "memory.core.updated",
					version: 1,
					actor,
					data: { slot, changedBy: actor },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	/**
	 * Compute a deterministic hash of all core memory slots. Used by the
	 * session manager (Phase 10) to detect when the system prompt needs
	 * refreshing because core memory has changed.
	 */
	async hash(): Promise<string> {
		const rows = await this.sql`
			SELECT md5(string_agg(slot || ':' || body::text, ',' ORDER BY slot)) AS hash
			FROM core_memory
		`;
		const row = rows[0];
		const hash = (row as Record<string, unknown> | undefined)?.["hash"];
		if (typeof hash !== "string") {
			throw new Error("Hash query returned NULL — core memory slots may be missing");
		}
		return hash;
	}
}
