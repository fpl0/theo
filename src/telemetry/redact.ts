/**
 * Attribute allowlist + redaction helpers.
 *
 * Theo is a *personal* agent — message bodies, tool arguments, node text,
 * and proposal payloads all contain sensitive data. Anything not on the
 * allowlist is replaced with `"[redacted]"` before it leaves the process.
 *
 * The allowlist is deliberately short. New attributes require a review.
 */

import * as Sem from "./semconv.ts";

// Prefix-match wildcard marker. `service.*` covers `service.name`, etc.
const PREFIXES: readonly string[] = ["service.", "host.", "process.", "code."];

/** Exact-match allowed keys — everything else must either match a prefix or be redacted. */
const EXACT = new Set<string>([
	Sem.ATTR_DB_SYSTEM,
	Sem.ATTR_DB_OPERATION,
	Sem.ATTR_DB_STATEMENT,
	Sem.ATTR_MESSAGING_SYSTEM,
	Sem.ATTR_MESSAGING_DESTINATION,
	Sem.ATTR_MESSAGING_OP,
	Sem.ATTR_THEO_GATE,
	Sem.ATTR_THEO_MODEL,
	Sem.ATTR_THEO_ROLE,
	Sem.ATTR_THEO_GOAL_ID,
	Sem.ATTR_THEO_PROPOSAL_ID,
	Sem.ATTR_THEO_TURN_CLASS,
	Sem.ATTR_THEO_EVENT_ID,
	Sem.ATTR_THEO_EVENT_TYPE,
	Sem.ATTR_THEO_EVENT_VERSION,
	Sem.ATTR_THEO_MESSAGE_LENGTH,
	Sem.ATTR_THEO_AUTONOMY_DOMAIN,
	Sem.ATTR_THEO_DEGRADATION_LEVEL,
	Sem.ATTR_THEO_TOKENS_INPUT,
	Sem.ATTR_THEO_TOKENS_OUTPUT,
	Sem.ATTR_THEO_COST_USD,
]);

/** Placeholder value written when a disallowed key is seen. */
export const REDACTED = "[redacted]";

/** True when `key` is on the allowlist (exact match or allowed prefix). */
export function isAllowed(key: string): boolean {
	if (EXACT.has(key)) return true;
	for (const p of PREFIXES) if (key.startsWith(p)) return true;
	return false;
}

/**
 * Return the key bucket the redaction counter should use — coarsened so a
 * misbehaving caller writing `user.message.${i}` doesn't explode cardinality
 * on the counter itself.
 */
export function coarsen(key: string): string {
	const dot = key.indexOf(".");
	return dot === -1 ? key : key.slice(0, dot);
}

/**
 * Coarsen `db.statement` down to operation + table name, stripping any
 * bound values. `SELECT * FROM node WHERE id = 42` → `"SELECT FROM node"`.
 * Unparseable inputs fall back to the operation token alone.
 */
export function coarsenDbStatement(stmt: string): string {
	const match = /^(SELECT|INSERT|UPDATE|DELETE|UPSERT|WITH)\b/iu.exec(stmt.trim());
	const op = match?.[1]?.toUpperCase() ?? "UNKNOWN";
	const tableMatch = /\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-zA-Z_][\w.]*)/u.exec(stmt);
	const table = tableMatch?.[1] ?? "?";
	return `${op} FROM ${table}`;
}

/**
 * Filter a record of attributes through the allowlist. Returns a fresh
 * object; the input is not mutated. Disallowed keys are replaced, not
 * dropped, so callers that rely on the key's presence still see something.
 */
export function redactAttributes(
	attrs: Readonly<Record<string, unknown>>,
	onReject?: (key: string) => void,
): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(attrs)) {
		if (!isAllowed(k)) {
			onReject?.(k);
			out[k] = REDACTED;
			continue;
		}
		if (k === Sem.ATTR_DB_STATEMENT && typeof v === "string") {
			out[k] = coarsenDbStatement(v);
			continue;
		}
		out[k] = v;
	}
	return out;
}
