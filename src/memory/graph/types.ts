/**
 * Type definitions for Theo's knowledge graph.
 *
 * Branded types (NodeId, EdgeId) prevent accidentally passing raw numbers
 * where a typed ID is expected. Factory functions are the sole entry point
 * for creating branded values — the single `as` cast in each is the allowed
 * exception per project convention.
 */

import type { Actor, NodeKind, Sensitivity } from "../../events/types.ts";
import type { JsonValue } from "../types.ts";

// ---------------------------------------------------------------------------
// Re-exports from event types (used by consumers of the graph module)
// ---------------------------------------------------------------------------

export type { NodeKind, Sensitivity } from "../../events/types.ts";

// ---------------------------------------------------------------------------
// Node metadata
// ---------------------------------------------------------------------------

/**
 * Structured attributes attached to a node. The body stays the
 * embeddable/searchable text -- metadata is advisory structure per kind
 * (e.g., `person.company`, `event.date`). Defaults to an empty object
 * in the database; callers can omit it entirely on create/update.
 */
export type NodeMetadata = { readonly [key: string]: JsonValue };

// ---------------------------------------------------------------------------
// Trust tiers
// ---------------------------------------------------------------------------

export type TrustTier =
	| "owner"
	| "owner_confirmed"
	| "verified"
	| "inferred"
	| "external"
	| "untrusted";

// ---------------------------------------------------------------------------
// Branded IDs
// ---------------------------------------------------------------------------

/** Branded integer ID for knowledge graph nodes. */
export type NodeId = number & { readonly __brand: "NodeId" };

/** Branded integer ID for knowledge graph edges. */
export type EdgeId = number & { readonly __brand: "EdgeId" };

/** Brand a raw number as a NodeId. The `as` cast is the one allowed exception. */
export function asNodeId(n: number): NodeId {
	return n as NodeId;
}

/** Brand a raw number as an EdgeId. The `as` cast is the one allowed exception. */
export function asEdgeId(n: number): EdgeId {
	return n as EdgeId;
}

// ---------------------------------------------------------------------------
// Edge labels
// ---------------------------------------------------------------------------

/**
 * Known edge labels. Open-typed via intersection with `string` so callers can
 * still introduce domain-specific labels, but the well-known set is centralized
 * here to catch typos at compile time.
 */
export type EdgeType =
	| "co_occurs"
	| "contradicts"
	| "abstracted_from"
	| "merged_into"
	| "related_to"
	| (string & { readonly __label?: "edge" });

// ---------------------------------------------------------------------------
// Node
// ---------------------------------------------------------------------------

export interface Node {
	readonly id: NodeId;
	readonly kind: NodeKind;
	readonly body: string;
	readonly embedding: Float32Array | null;
	readonly trust: TrustTier;
	readonly confidence: number;
	readonly importance: number;
	readonly sensitivity: Sensitivity;
	readonly accessCount: number;
	readonly lastAccessedAt: Date | null;
	readonly createdAt: Date;
	readonly updatedAt: Date;
	/** Structured attributes per kind (advisory; body stays authoritative). */
	readonly metadata: NodeMetadata;
	/** ULID of the `memory.node.created` event that produced this node, if any. */
	readonly sourceEventId: string | null;
}

/** Input for creating a new node. */
export interface CreateNodeInput {
	readonly kind: NodeKind;
	readonly body: string;
	readonly trust?: TrustTier;
	readonly confidence?: number;
	readonly importance?: number;
	readonly sensitivity?: Sensitivity;
	readonly actor: Actor;
	/** Event metadata attached to the emitted `memory.node.created`. */
	readonly metadata?: Record<string, unknown>;
	/** Structured node attributes persisted to `node.metadata`. */
	readonly nodeMetadata?: NodeMetadata;
}

/** Input for updating an existing node. Only provided fields are changed. */
export interface UpdateNodeInput {
	readonly kind?: NodeKind;
	readonly body?: string;
	readonly trust?: TrustTier;
	readonly confidence?: number;
	readonly importance?: number;
	readonly sensitivity?: Sensitivity;
	readonly actor: Actor;
	/** Event metadata attached to the emitted `memory.node.updated`. */
	readonly metadata?: Record<string, unknown>;
	/** Replacement structured attributes (partial updates not supported). */
	readonly nodeMetadata?: NodeMetadata;
}

// ---------------------------------------------------------------------------
// Edge
// ---------------------------------------------------------------------------

export interface Edge {
	readonly id: EdgeId;
	readonly sourceId: NodeId;
	readonly targetId: NodeId;
	readonly label: string;
	readonly weight: number;
	readonly validFrom: Date;
	readonly validTo: Date | null;
	readonly createdAt: Date;
}

/** Input for creating a new edge. */
export interface CreateEdgeInput {
	readonly sourceId: NodeId;
	readonly targetId: NodeId;
	readonly label: string;
	readonly weight?: number;
	readonly actor: Actor;
	readonly metadata?: Record<string, unknown>;
}

/** Input for updating an edge (expire old + create new with updated fields). */
export interface UpdateEdgeInput {
	readonly label?: string;
	readonly weight?: number;
	readonly actor: Actor;
	readonly metadata?: Record<string, unknown>;
}
