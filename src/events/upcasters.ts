/**
 * Upcaster registry for event schema evolution.
 *
 * Events are immutable -- when a schema changes, upcasters transform old shapes
 * to new ones at read time. The registry manages version chains and validates
 * that no gaps exist before any event replay begins.
 */

/** An upcaster transforms event data from one version to the next. */
export type Upcaster = (data: Record<string, unknown>) => Record<string, unknown>;

/** Read-only view of current schema versions for all event types. */
export type CurrentVersions = ReadonlyMap<string, number>;

/** A gap detected during chain validation. */
export interface ValidationGap {
	readonly eventType: string;
	readonly missingVersion: number;
}

/**
 * Registry interface for managing event schema evolution.
 *
 * Call validate() at startup before any event replay to detect missing
 * chain links. A missing link means events at that version cannot be
 * upcast to the current version, which would cause data loss.
 */
export interface UpcasterRegistry {
	/**
	 * Register an upcaster that transforms data from `fromVersion` to `fromVersion + 1`.
	 * Automatically updates currentVersions to `fromVersion + 1` if that is higher
	 * than the current recorded version.
	 */
	register(eventType: string, fromVersion: number, fn: Upcaster): void;

	/**
	 * Apply all upcasters in sequence from `fromVersion` up to the current version
	 * recorded in currentVersions for this event type.
	 * If `fromVersion` equals the current version, returns data unchanged.
	 * If the event type is unknown, returns data unchanged.
	 */
	upcast(
		eventType: string,
		fromVersion: number,
		data: Record<string, unknown>,
	): Record<string, unknown>;

	/**
	 * Validate that all registered chains are contiguous (no gaps).
	 * Call at startup before any event replay.
	 * Returns a list of missing links, empty if all chains are valid.
	 */
	validate(): ReadonlyArray<ValidationGap>;

	/** Read-only access to the current version map. */
	readonly currentVersions: CurrentVersions;
}

/**
 * Create a new upcaster registry.
 *
 * The version map starts empty. Any event type without a registered upcaster
 * is implicitly at version 1 -- no need to pre-populate with all known types.
 * Entries are created automatically when register() is called.
 */
export function createUpcasterRegistry(): UpcasterRegistry {
	const versions = new Map<string, number>();

	// Upcaster storage: Map<"eventType::fromVersion", Upcaster>
	const upcasters = new Map<string, Upcaster>();

	function key(eventType: string, fromVersion: number): string {
		return `${eventType}::${String(fromVersion)}`;
	}

	function register(eventType: string, fromVersion: number, fn: Upcaster): void {
		upcasters.set(key(eventType, fromVersion), fn);
		const targetVersion = fromVersion + 1;
		const current = versions.get(eventType);
		if (current === undefined || targetVersion > current) {
			versions.set(eventType, targetVersion);
		}
	}

	function upcast(
		eventType: string,
		fromVersion: number,
		data: Record<string, unknown>,
	): Record<string, unknown> {
		// Implicit version 1 for types without registered upcasters
		const currentVersion = versions.get(eventType) ?? 1;
		if (fromVersion >= currentVersion) {
			return data;
		}

		let result = data;
		for (let v = fromVersion; v < currentVersion; v++) {
			const fn = upcasters.get(key(eventType, v));
			if (fn !== undefined) {
				result = fn(result);
			}
			// If no upcaster registered for this step, data passes through.
			// validate() will catch this gap at startup.
		}
		return result;
	}

	function validate(): ReadonlyArray<ValidationGap> {
		const gaps: ValidationGap[] = [];

		// For each event type that has upcasters (version > 1), check chain continuity
		for (const [eventType, currentVersion] of versions) {
			if (currentVersion <= 1) {
				continue;
			}
			// Check every step from 1 to currentVersion - 1
			for (let v = 1; v < currentVersion; v++) {
				if (!upcasters.has(key(eventType, v))) {
					gaps.push({ eventType, missingVersion: v });
				}
			}
		}

		return gaps;
	}

	return {
		register,
		upcast,
		validate,
		get currentVersions(): CurrentVersions {
			return versions;
		},
	};
}
