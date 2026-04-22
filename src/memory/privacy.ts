/**
 * Privacy filter: pure functions for sensitivity detection and trust enforcement.
 *
 * The privacy filter is a gate, not a filter. It rejects at the storage boundary,
 * before data enters the immutable event log. Every function here is pure — no
 * database, no state, no side effects. This makes edge cases trivially testable.
 *
 * Sensitivity is classified into three tiers ordered by exploitability:
 *   - restricted: financial (SSN, credit cards, IBAN) and medical (diagnoses,
 *     prescriptions, ICD codes) — direct fraud risk or heavy regulation
 *   - sensitive: identity (passport, driver's license), location (addresses,
 *     GPS), email, phone — exploitable with effort
 *   - none: no sensitivity concern
 *
 * Trust enforcement compares detected sensitivity against the maximum allowed
 * for the actor's trust tier.
 *
 * Integration: Phase 9/10 wire this into repository-level storage paths.
 * The filter must be called at the repository level (not just hooks) so
 * no code path can bypass it.
 */

import type { Sensitivity } from "../events/types.ts";
import type { TrustTier } from "./graph/types.ts";

// Re-export for consumers
export type { Sensitivity } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Sensitivity Levels
// ---------------------------------------------------------------------------

/** Numeric levels for comparison. Higher = more exploitable. */
const SENSITIVITY_LEVEL: Record<Sensitivity, number> = {
	none: 0,
	sensitive: 1,
	restricted: 2,
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Maximum content length to scan. Content beyond this is truncated before
 * regex scanning to prevent performance degradation on large inputs.
 * 100KB is well beyond any realistic single memory node or message.
 */
const MAX_SCAN_LENGTH = 100_000;

// ---------------------------------------------------------------------------
// Sensitivity Detection
// ---------------------------------------------------------------------------

export interface SensitivityMatch {
	readonly tier: Sensitivity;
	readonly label: string;
}

const SENSITIVITY_PATTERNS: ReadonlyArray<{
	readonly tier: Sensitivity;
	readonly pattern: RegExp;
	readonly label: string;
}> = [
	// Restricted — direct fraud risk or heavy regulation

	// SSN: handles hyphenated (123-45-6789), spaces, dots, and contiguous formats
	{ tier: "restricted", pattern: /\b\d{3}[\s.-]?\d{2}[\s.-]?\d{4}\b/, label: "SSN" },

	// Credit cards: handles contiguous, spaced, and dashed formats
	// Visa: starts with 4, 13 or 16 digits
	{
		tier: "restricted",
		pattern: /\b4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b/,
		label: "credit card",
	},
	// Visa 13-digit (older)
	{
		tier: "restricted",
		pattern: /\b4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d\b/,
		label: "credit card",
	},
	// Mastercard: starts with 51-55
	{
		tier: "restricted",
		pattern: /\b5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b/,
		label: "credit card",
	},
	// Amex: starts with 34 or 37, 15 digits (4-6-5 grouping)
	{
		tier: "restricted",
		pattern: /\b3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}\b/,
		label: "credit card",
	},

	// IBAN: 2 uppercase letters + 2 digits + 4 alphanumeric + 7+ digits + optional suffix
	{
		tier: "restricted",
		pattern: /\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}[A-Z0-9]{0,16}\b/,
		label: "IBAN",
	},

	// Diagnosis: bounded gap (50 chars max between "diagnosed" and "with/of")
	{
		tier: "restricted",
		pattern: /\bdiagnos(?:ed|is)\b.{0,50}\b(?:with|of)\b/i,
		label: "diagnosis",
	},
	{
		tier: "restricted",
		pattern: /\b(?:prescribed?|prescription|dosage|mg\/day)\b/i,
		label: "prescription",
	},

	// ICD code: requires dot suffix to avoid false positives on "B12", "F22" etc.
	{ tier: "restricted", pattern: /\b[A-Z]\d{2}\.\d{1,4}\b/, label: "ICD code" },

	// Sensitive — exploitable with effort
	{
		tier: "sensitive",
		pattern: /\bpassport\s*(?:#|no|number)?\s*[:.]?\s*[A-Z0-9]{6,9}\b/i,
		label: "passport",
	},
	{
		tier: "sensitive",
		pattern: /\b(?:driver'?s?\s*licen[cs]e|DL)\s*(?:#|no|number)?\s*[:.]?\s*[A-Z0-9]{5,15}\b/i,
		label: "drivers license",
	},

	// Street address: case-insensitive to catch "123 main st"
	{
		tier: "sensitive",
		pattern:
			/\b\d{1,5}\s+[A-Za-z]+(?:\s+[A-Za-z]+)*\s+(?:St|Ave|Blvd|Rd|Dr|Ln|Way|Ct|Place|Circle)\b/i,
		label: "street address",
	},

	// GPS coordinates: 2+ decimal places is ~1km precision (identifying for home address)
	{
		tier: "sensitive",
		pattern: /-?\d{1,3}\.\d{2,},?\s*-?\d{1,3}\.\d{2,}/,
		label: "GPS coordinates",
	},

	// Email address
	{
		tier: "sensitive",
		pattern: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/,
		label: "email address",
	},

	// Phone number: international and common US/EU formats
	{
		tier: "sensitive",
		pattern: /(?:\+\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{4}\b/,
		label: "phone number",
	},
];

/**
 * Scan content for sensitive data patterns.
 * Returns the highest-severity match, or { tier: "none", label: "none" } if clean.
 * Pure function — no state, no side effects.
 *
 * Content beyond MAX_SCAN_LENGTH is truncated before scanning to prevent
 * performance degradation on very large inputs.
 */
export function detectSensitivity(content: string): SensitivityMatch {
	const scanContent =
		content.length > MAX_SCAN_LENGTH ? content.slice(0, MAX_SCAN_LENGTH) : content;

	let highest: SensitivityMatch = { tier: "none", label: "none" };
	let highestLevel = 0;

	for (const entry of SENSITIVITY_PATTERNS) {
		if (entry.pattern.test(scanContent)) {
			const level = SENSITIVITY_LEVEL[entry.tier];
			if (level > highestLevel) {
				highest = { tier: entry.tier, label: entry.label };
				highestLevel = level;
				// restricted is the max tier — no need to check remaining patterns
				if (highestLevel === 2) return highest;
			}
		}
	}

	return highest;
}

// ---------------------------------------------------------------------------
// Trust Tier Enforcement
// ---------------------------------------------------------------------------

export type PrivacyDecision =
	| { readonly allowed: true }
	| { readonly allowed: false; readonly reason: string; readonly tier: Sensitivity };

/** Maximum sensitivity each trust tier is allowed to store. */
const TRUST_SENSITIVITY_MAP: Record<TrustTier, Sensitivity> = {
	owner: "restricted",
	owner_confirmed: "restricted",
	verified: "sensitive",
	inferred: "none",
	external: "none",
	untrusted: "none",
};

/**
 * Gate function: should this content be allowed into the event log?
 * Pure function — no database, no state, no side effects.
 *
 * Compares detected sensitivity against the maximum allowed for the
 * **effective** trust tier — the min of the actor's tier and every
 * ancestor event's effective tier (see `foundation.md §7.3` and
 * `src/memory/trust.ts`). Callers inside Phase 13b threading path pass the
 * effective tier from event metadata; legacy callers can pass the actor's
 * tier and will get a (weakly) correct decision.
 */
export function checkPrivacy(content: string, effectiveTrust: TrustTier): PrivacyDecision {
	const detected = detectSensitivity(content);
	const maxAllowed = TRUST_SENSITIVITY_MAP[effectiveTrust];

	if (SENSITIVITY_LEVEL[detected.tier] > SENSITIVITY_LEVEL[maxAllowed]) {
		return {
			allowed: false,
			reason:
				`Content contains ${detected.label} (${detected.tier}), ` +
				`which exceeds the ${effectiveTrust} trust tier limit (max: ${maxAllowed})`,
			tier: detected.tier,
		};
	}

	return { allowed: true };
}
