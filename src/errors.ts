/**
 * Result type and error hierarchy for Theo.
 *
 * Errors are values, not exceptions. Every fallible operation returns Result<T, E>.
 * The AppError union grows with each phase -- discriminate on `code`.
 */

/** Structured validation issue from Zod parsing. */
export interface ValidationIssue {
	readonly path: string;
	readonly message: string;
}

/**
 * Discriminated union of all application errors.
 * Each variant carries a unique `code` for exhaustive switch/case handling.
 */
export type AppError =
	| {
			readonly code: "CONFIG_INVALID";
			readonly message: string;
			readonly issues: readonly ValidationIssue[];
	  }
	| { readonly code: "DB_CONNECTION_FAILED"; readonly message: string }
	| { readonly code: "MIGRATION_FAILED"; readonly migration: string; readonly message: string };

/**
 * Result type: either a success with a value, or a failure with an error.
 * Both branches are readonly to prevent mutation after creation.
 */
export type Result<T, E = AppError> =
	| { readonly ok: true; readonly value: T }
	| { readonly ok: false; readonly error: E };

/** Construct a success result. */
export function ok<T>(value: T): Result<T, never> {
	return { ok: true, value };
}

/** Construct a failure result. */
export function err<E>(error: E): Result<never, E> {
	return { ok: false, error };
}

/** Type guard: narrows a Result to its success branch. */
export function isOk<T, E>(
	result: Result<T, E>,
): result is { readonly ok: true; readonly value: T } {
	return result.ok;
}

/** Type guard: narrows a Result to its failure branch. */
export function isErr<T, E>(
	result: Result<T, E>,
): result is { readonly ok: false; readonly error: E } {
	return !result.ok;
}

/** Extract a human-readable message from an unknown thrown value. */
export function describeError(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
