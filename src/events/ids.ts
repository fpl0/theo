/**
 * Branded EventId type wrapping ULID.
 *
 * The brand prevents passing arbitrary strings where an EventId is expected.
 * The single `as` cast in newEventId() is the one exception allowed by convention --
 * branding requires it.
 */

import { monotonicFactory } from "ulid";

/** Branded string type for event identifiers. Backed by ULID for sortable, timestamp-embedded IDs. */
export type EventId = string & { readonly __brand: "EventId" };

// Monotonic factory ensures ULIDs generated within the same millisecond are
// lexicographically increasing. Without this, ulid() uses random suffixes that
// are NOT guaranteed to be monotonic, breaking ORDER BY id and ULID dedup.
const ulid = monotonicFactory();

/** Create a new EventId from a fresh ULID. */
export function newEventId(): EventId {
	// This `as` cast is the single exception -- branding requires it.
	// The ULID library returns `string`, and we brand it exactly once here.
	return ulid() as EventId;
}
