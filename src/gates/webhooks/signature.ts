/**
 * HMAC signature verification for webhook gates.
 *
 * Every supported source uses HMAC-SHA256 over the raw body with a shared
 * secret. The comparison MUST use `timingSafeEqual` — never `===` or
 * `equals()`. A single wall-clock leak on the hot compare path is a
 * signature-bypass vulnerability.
 *
 * The verifier supports rotation: if the current secret does not verify,
 * fall back to the previous secret during the grace window. Both paths use
 * constant-time compare so neither branch leaks timing information about
 * which secret matched (or that either matched).
 */

import { createHmac, timingSafeEqual } from "node:crypto";

// ---------------------------------------------------------------------------
// Shared verifier shape
// ---------------------------------------------------------------------------

export interface SecretPair {
	readonly current: string;
	/** Previous secret during the rotation grace window. Null = not rotated recently. */
	readonly previous: string | null;
}

/**
 * Verify an HMAC signature against one or both secrets in the pair. Returns
 * true only when the signature matches. All code paths use `timingSafeEqual`.
 */
export type Verifier = (body: Buffer, header: string, secrets: SecretPair) => boolean;

// ---------------------------------------------------------------------------
// Constant-time compare helper
// ---------------------------------------------------------------------------

/**
 * Compare two strings in constant time. Returns false immediately on length
 * mismatch (the length alone does not leak the secret — HMAC output lengths
 * are public per algorithm). The `Buffer.from` / `timingSafeEqual` pair is
 * the standard Node.js idiom for this.
 */
export function constantTimeEquals(a: string, b: string): boolean {
	if (a.length !== b.length) return false;
	const aBuf = Buffer.from(a);
	const bBuf = Buffer.from(b);
	if (aBuf.length !== bBuf.length) return false;
	return timingSafeEqual(aBuf, bBuf);
}

// ---------------------------------------------------------------------------
// GitHub
// ---------------------------------------------------------------------------

/**
 * GitHub signs payloads with HMAC-SHA256. The header is `x-hub-signature-256`
 * formatted as `sha256=<hex>`. Returns true iff the signature matches
 * either the current or previous secret.
 */
export function verifyGithub(body: Buffer, header: string, secrets: SecretPair): boolean {
	if (!header.startsWith("sha256=")) return false;
	const currentExpected = `sha256=${createHmac("sha256", secrets.current).update(body).digest("hex")}`;
	if (constantTimeEquals(currentExpected, header)) return true;
	if (secrets.previous !== null) {
		const previousExpected = `sha256=${createHmac("sha256", secrets.previous).update(body).digest("hex")}`;
		return constantTimeEquals(previousExpected, header);
	}
	return false;
}

// ---------------------------------------------------------------------------
// Linear
// ---------------------------------------------------------------------------

/**
 * Linear signs payloads with HMAC-SHA256. The header is `linear-signature`
 * formatted as raw hex (no prefix).
 */
export function verifyLinear(body: Buffer, header: string, secrets: SecretPair): boolean {
	const currentExpected = createHmac("sha256", secrets.current).update(body).digest("hex");
	if (constantTimeEquals(currentExpected, header)) return true;
	if (secrets.previous !== null) {
		const previousExpected = createHmac("sha256", secrets.previous).update(body).digest("hex");
		return constantTimeEquals(previousExpected, header);
	}
	return false;
}

// ---------------------------------------------------------------------------
// Email relay
// ---------------------------------------------------------------------------

/**
 * Email relays (e.g., a trusted forwarder the owner runs) sign messages with
 * a Theo-managed HMAC secret. Header format is the same as Linear — raw hex.
 * Theo only supports signed email; unsigned inbound email is rejected at
 * the source allowlist level.
 */
export function verifyEmailRelay(body: Buffer, header: string, secrets: SecretPair): boolean {
	return verifyLinear(body, header, secrets);
}

// ---------------------------------------------------------------------------
// Source dispatch
// ---------------------------------------------------------------------------

export type KnownSource = "github" | "linear" | "email";

const VERIFIERS: Readonly<Record<KnownSource, Verifier>> = {
	github: verifyGithub,
	linear: verifyLinear,
	email: verifyEmailRelay,
};

export function verifierFor(source: string): Verifier | null {
	if (source in VERIFIERS) {
		return VERIFIERS[source as KnownSource];
	}
	return null;
}
