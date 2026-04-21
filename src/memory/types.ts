/**
 * Shared type definitions for Theo's memory system.
 *
 * JsonValue provides type-safe JSONB representation — no `unknown` for structured data.
 * Episode and CoreMemory types are used by the episodic and core memory repositories.
 */

import type { Actor, CoreMemorySlot, MessageRole } from "../events/types.ts";

export type { CoreMemorySlot, MessageRole };

// ---------------------------------------------------------------------------
// JSON value types for JSONB columns
// ---------------------------------------------------------------------------

type JsonPrimitive = string | number | boolean | null;

/**
 * Type-safe JSON value. All JSONB data stored in PostgreSQL uses this type
 * instead of `unknown`, providing structural type safety while remaining
 * flexible enough for arbitrary JSON documents.
 */
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

// ---------------------------------------------------------------------------
// Episode types
// ---------------------------------------------------------------------------

/** Branded episode ID to prevent mixing with node/edge IDs. */
export type EpisodeId = number & { readonly __brand: "EpisodeId" };

/** Narrow a plain number to EpisodeId. */
export function asEpisodeId(n: number): EpisodeId {
	return n as EpisodeId;
}

/**
 * A single episodic memory entry — one message in a conversation.
 *
 * Episodes are append-only: never UPDATE an episode's body. Consolidation
 * (Phase 13) creates a new summary episode and sets `superseded_by` on the
 * originals.
 */
export interface Episode {
	readonly id: EpisodeId;
	readonly sessionId: string;
	readonly role: MessageRole;
	readonly body: string;
	readonly embedding: Float32Array | null;
	readonly supersededBy: EpisodeId | null;
	readonly createdAt: Date;
	/** Salience score in [0, 1]; gates background consolidation. */
	readonly importance: number;
}

/** Input for creating a new episode. */
export interface CreateEpisodeInput {
	readonly sessionId: string;
	readonly role: MessageRole;
	readonly body: string;
	readonly actor: Actor;
	/**
	 * Optional salience score. Defaults to 0.5 (the schema default). Use
	 * `scoreEpisodeImportance` from `./salience.ts` to compute a value
	 * from structured turn signals.
	 */
	readonly importance?: number;
}

// ---------------------------------------------------------------------------
// Core memory types
// ---------------------------------------------------------------------------

/**
 * All core memory slots loaded together. Core memory is always in the system
 * prompt, never truncated. The hash of all slots determines when the system
 * prompt needs refreshing.
 */
export interface CoreMemory {
	readonly persona: JsonValue;
	readonly goals: JsonValue;
	readonly userModel: JsonValue;
	readonly context: JsonValue;
}

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

/**
 * Returned when a core memory slot is not found in the database.
 * Slots are seeded by migration, so this indicates data corruption,
 * a manual DELETE, or a bad migration — defensive for decade-long operation.
 */
export class SlotNotFoundError extends Error {
	readonly slot: CoreMemorySlot;
	constructor(slot: CoreMemorySlot) {
		super(`Core memory slot not found: ${slot}`);
		this.name = "SlotNotFoundError";
		this.slot = slot;
	}
}
