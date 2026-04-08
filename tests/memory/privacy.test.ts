/**
 * Unit tests for the privacy filter.
 *
 * Tests sensitivity detection (regex heuristics) and trust tier enforcement.
 * Pure function tests — no database, no mocks needed. This is the most
 * thorough test suite in the project because the privacy filter is the last
 * line of defense before immutable storage.
 */

import { describe, expect, test } from "bun:test";
import { checkPrivacy, detectSensitivity, type PrivacyDecision } from "../../src/memory/privacy.ts";

// ---------------------------------------------------------------------------
// detectSensitivity
// ---------------------------------------------------------------------------

describe("detectSensitivity", () => {
	test("clean text returns none", () => {
		expect(detectSensitivity("User likes coffee")).toEqual({ tier: "none", label: "none" });
	});

	test("empty text returns none", () => {
		expect(detectSensitivity("")).toEqual({ tier: "none", label: "none" });
	});

	test("numbers in context are not flagged", () => {
		expect(detectSensitivity("Room 123, Floor 4")).toEqual({ tier: "none", label: "none" });
	});

	test("partial SSN is not flagged", () => {
		expect(detectSensitivity("123-45")).toEqual({ tier: "none", label: "none" });
	});

	// --- SSN patterns (restricted) ---

	test("SSN: hyphenated format", () => {
		expect(detectSensitivity("SSN: 123-45-6789")).toEqual({
			tier: "restricted",
			label: "SSN",
		});
	});

	test("SSN: contiguous format", () => {
		expect(detectSensitivity("my SSN is 123456789")).toEqual({
			tier: "restricted",
			label: "SSN",
		});
	});

	test("SSN: space-separated format", () => {
		expect(detectSensitivity("SSN 123 45 6789")).toEqual({
			tier: "restricted",
			label: "SSN",
		});
	});

	test("SSN: dot-separated format", () => {
		expect(detectSensitivity("SSN 123.45.6789")).toEqual({
			tier: "restricted",
			label: "SSN",
		});
	});

	// --- Credit card patterns (restricted) ---

	test("credit card Visa: contiguous", () => {
		expect(detectSensitivity("card 4111111111111111")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Visa: spaces", () => {
		expect(detectSensitivity("card 4111 1111 1111 1111")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Visa: dashes", () => {
		expect(detectSensitivity("card 4111-1111-1111-1111")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Mastercard: contiguous", () => {
		expect(detectSensitivity("pay with 5500000000000004")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Mastercard: spaces", () => {
		expect(detectSensitivity("pay with 5500 0000 0000 0004")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Amex: contiguous", () => {
		expect(detectSensitivity("amex 340000000000009")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	test("credit card Amex: spaces", () => {
		expect(detectSensitivity("amex 3400 000000 00009")).toEqual({
			tier: "restricted",
			label: "credit card",
		});
	});

	// --- Other restricted patterns ---

	test("IBAN", () => {
		const iban = ["GB29", "NWBK", "60161331926819"].join("");
		expect(detectSensitivity(`transfer to ${iban}`)).toEqual({
			tier: "restricted",
			label: "IBAN",
		});
	});

	test("diagnosis: diagnosed with", () => {
		expect(detectSensitivity("diagnosed with diabetes")).toEqual({
			tier: "restricted",
			label: "diagnosis",
		});
	});

	test("diagnosis: diagnosis of", () => {
		expect(detectSensitivity("a diagnosis of lupus was confirmed")).toEqual({
			tier: "restricted",
			label: "diagnosis",
		});
	});

	test("diagnosis: gap > 50 chars is NOT flagged", () => {
		const filler = "x".repeat(60);
		expect(detectSensitivity(`diagnosed ${filler} with something`).tier).toBe("none");
	});

	test("prescription", () => {
		expect(detectSensitivity("prescribed 50mg/day")).toEqual({
			tier: "restricted",
			label: "prescription",
		});
	});

	test("dosage pattern", () => {
		expect(detectSensitivity("current dosage is 200mg")).toEqual({
			tier: "restricted",
			label: "prescription",
		});
	});

	test("ICD code with dot suffix", () => {
		expect(detectSensitivity("code E11.65 in chart")).toEqual({
			tier: "restricted",
			label: "ICD code",
		});
	});

	// --- ICD false positive prevention ---

	test("bare B12 is NOT flagged as ICD (no dot)", () => {
		expect(detectSensitivity("taking B12 supplements")).toEqual({
			tier: "none",
			label: "none",
		});
	});

	test("F22 aircraft is NOT flagged as ICD (no dot)", () => {
		expect(detectSensitivity("the F22 Raptor is fast")).toEqual({
			tier: "none",
			label: "none",
		});
	});

	// --- Sensitive tier ---

	test("passport number", () => {
		expect(detectSensitivity("passport #AB1234567")).toEqual({
			tier: "sensitive",
			label: "passport",
		});
	});

	test("drivers license", () => {
		expect(detectSensitivity("driver's license DL12345678")).toEqual({
			tier: "sensitive",
			label: "drivers license",
		});
	});

	test("street address: capitalized", () => {
		expect(detectSensitivity("lives at 123 Main St")).toEqual({
			tier: "sensitive",
			label: "street address",
		});
	});

	test("street address: lowercase", () => {
		expect(detectSensitivity("lives at 123 main st")).toEqual({
			tier: "sensitive",
			label: "street address",
		});
	});

	test("GPS coordinates: high precision (4+ decimals)", () => {
		expect(detectSensitivity("37.7749, -122.4194")).toEqual({
			tier: "sensitive",
			label: "GPS coordinates",
		});
	});

	test("GPS coordinates: low precision (2 decimals)", () => {
		expect(detectSensitivity("location is 37.77, -122.42")).toEqual({
			tier: "sensitive",
			label: "GPS coordinates",
		});
	});

	test("email address", () => {
		expect(detectSensitivity("contact me at user@example.com")).toEqual({
			tier: "sensitive",
			label: "email address",
		});
	});

	test("phone number: US format", () => {
		expect(detectSensitivity("call me at (555) 123-4567")).toEqual({
			tier: "sensitive",
			label: "phone number",
		});
	});

	test("phone number: international format", () => {
		expect(detectSensitivity("reach me at +44 20 7946 0958")).toEqual({
			tier: "sensitive",
			label: "phone number",
		});
	});

	// --- Highest severity wins ---

	test("multiple patterns: highest severity wins (SSN + address)", () => {
		expect(detectSensitivity("SSN 123-45-6789 at 123 Main St").tier).toBe("restricted");
	});

	test("multiple patterns: highest severity wins (credit card + GPS)", () => {
		expect(detectSensitivity("card 4111111111111111 at 37.7749, -122.4194").tier).toBe(
			"restricted",
		);
	});
});

// ---------------------------------------------------------------------------
// checkPrivacy
// ---------------------------------------------------------------------------

describe("checkPrivacy", () => {
	// --- Clean text passes all tiers ---

	test("clean text, inferred tier: allowed", () => {
		expect(checkPrivacy("User likes coffee", "inferred").allowed).toBe(true);
	});

	test("clean text, untrusted tier: allowed", () => {
		expect(checkPrivacy("User likes coffee", "untrusted").allowed).toBe(true);
	});

	test("empty text, untrusted tier: allowed", () => {
		expect(checkPrivacy("", "untrusted").allowed).toBe(true);
	});

	// --- Owner can store anything ---

	test("SSN, owner: allowed", () => {
		expect(checkPrivacy("SSN: 123-45-6789", "owner").allowed).toBe(true);
	});

	test("SSN, owner_confirmed: allowed", () => {
		expect(checkPrivacy("SSN: 123-45-6789", "owner_confirmed").allowed).toBe(true);
	});

	test("medical, owner: allowed", () => {
		expect(checkPrivacy("diagnosed with diabetes", "owner").allowed).toBe(true);
	});

	// --- Verified tier: up to sensitive ---

	test("GPS, verified: allowed (sensitive <= sensitive)", () => {
		expect(checkPrivacy("37.7749, -122.4194", "verified").allowed).toBe(true);
	});

	test("email, verified: allowed (sensitive <= sensitive)", () => {
		expect(checkPrivacy("user@example.com", "verified").allowed).toBe(true);
	});

	test("SSN, verified: blocked (restricted > sensitive)", () => {
		const result = checkPrivacy("SSN: 123-45-6789", "verified");
		expect(result.allowed).toBe(false);
		assertBlocked(result);
		expect(result.tier).toBe("restricted");
		expect(result.reason).toContain("SSN");
		expect(result.reason).toContain("restricted");
		expect(result.reason).toContain("verified");
	});

	test("medical, verified: blocked (restricted > sensitive)", () => {
		expect(checkPrivacy("diagnosed with diabetes", "verified").allowed).toBe(false);
	});

	// --- Inferred/external/untrusted tiers: none only ---

	test("SSN, inferred: blocked", () => {
		expect(checkPrivacy("SSN: 123-45-6789", "inferred").allowed).toBe(false);
	});

	test("SSN, external: blocked", () => {
		expect(checkPrivacy("SSN: 123-45-6789", "external").allowed).toBe(false);
	});

	test("GPS, inferred: blocked (sensitive > none)", () => {
		const result = checkPrivacy("37.7749, -122.4194", "inferred");
		expect(result.allowed).toBe(false);
		assertBlocked(result);
		expect(result.tier).toBe("sensitive");
	});

	test("email, inferred: blocked (sensitive > none)", () => {
		expect(checkPrivacy("user@example.com", "inferred").allowed).toBe(false);
	});

	test("address, external: blocked", () => {
		expect(checkPrivacy("lives at 123 Main St", "external").allowed).toBe(false);
	});

	test("address, untrusted: blocked", () => {
		expect(checkPrivacy("lives at 123 Main St", "untrusted").allowed).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Type guard to narrow a PrivacyDecision to the blocked variant. */
function assertBlocked(
	decision: PrivacyDecision,
): asserts decision is Extract<PrivacyDecision, { readonly allowed: false }> {
	if (decision.allowed) {
		throw new Error("Expected blocked decision, got allowed");
	}
}
