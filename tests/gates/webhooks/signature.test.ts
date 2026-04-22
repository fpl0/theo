/**
 * Webhook signature verification tests.
 *
 * Every verifier uses `timingSafeEqual` under the hood. These tests verify
 * both positive (valid signature passes) and negative (length mismatch,
 * altered body, invalid prefix) cases, plus the rotation grace period.
 */

import { describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import {
	constantTimeEquals,
	verifierFor,
	verifyEmailRelay,
	verifyGithub,
	verifyLinear,
} from "../../../src/gates/webhooks/signature.ts";

function signGithub(body: Buffer, secret: string): string {
	return `sha256=${createHmac("sha256", secret).update(body).digest("hex")}`;
}

function signLinear(body: Buffer, secret: string): string {
	return createHmac("sha256", secret).update(body).digest("hex");
}

const SECRET_CURRENT = "s3cret-current";
const SECRET_PREVIOUS = "s3cret-previous";
const body = Buffer.from('{"hello":"world"}');

describe("constantTimeEquals", () => {
	test("equal strings return true", () => {
		expect(constantTimeEquals("abc", "abc")).toBe(true);
	});
	test("differing same-length strings return false", () => {
		expect(constantTimeEquals("abc", "abd")).toBe(false);
	});
	test("length mismatch returns false fast (no timing leak)", () => {
		expect(constantTimeEquals("a", "abc")).toBe(false);
	});
	test("empty strings are equal", () => {
		expect(constantTimeEquals("", "")).toBe(true);
	});
});

describe("verifyGithub", () => {
	test("valid signature with current secret passes", () => {
		const header = signGithub(body, SECRET_CURRENT);
		expect(verifyGithub(body, header, { current: SECRET_CURRENT, previous: null })).toBe(true);
	});

	test("invalid signature rejected", () => {
		const header = signGithub(body, "wrong-secret");
		expect(verifyGithub(body, header, { current: SECRET_CURRENT, previous: null })).toBe(false);
	});

	test("missing sha256= prefix rejected", () => {
		const hex = createHmac("sha256", SECRET_CURRENT).update(body).digest("hex");
		expect(verifyGithub(body, hex, { current: SECRET_CURRENT, previous: null })).toBe(false);
	});

	test("rotation grace: previous secret accepts", () => {
		const header = signGithub(body, SECRET_PREVIOUS);
		expect(verifyGithub(body, header, { current: SECRET_CURRENT, previous: SECRET_PREVIOUS })).toBe(
			true,
		);
	});

	test("altered body after signing rejected", () => {
		const header = signGithub(body, SECRET_CURRENT);
		const altered = Buffer.from('{"hello":"evil"}');
		expect(verifyGithub(altered, header, { current: SECRET_CURRENT, previous: null })).toBe(false);
	});
});

describe("verifyLinear", () => {
	test("hex signature with current secret passes", () => {
		const header = signLinear(body, SECRET_CURRENT);
		expect(verifyLinear(body, header, { current: SECRET_CURRENT, previous: null })).toBe(true);
	});
	test("wrong-length header rejected fast", () => {
		expect(verifyLinear(body, "short", { current: SECRET_CURRENT, previous: null })).toBe(false);
	});
	test("rotation grace accepts previous", () => {
		const header = signLinear(body, SECRET_PREVIOUS);
		expect(verifyLinear(body, header, { current: SECRET_CURRENT, previous: SECRET_PREVIOUS })).toBe(
			true,
		);
	});
});

describe("verifyEmailRelay", () => {
	test("same scheme as Linear", () => {
		const header = signLinear(body, SECRET_CURRENT);
		expect(verifyEmailRelay(body, header, { current: SECRET_CURRENT, previous: null })).toBe(true);
	});
});

describe("verifierFor", () => {
	test("known sources return a verifier", () => {
		expect(verifierFor("github")).not.toBeNull();
		expect(verifierFor("linear")).not.toBeNull();
		expect(verifierFor("email")).not.toBeNull();
	});
	test("unknown sources return null (config-level allowlist bypass prevented)", () => {
		expect(verifierFor("evil-source")).toBeNull();
		expect(verifierFor("")).toBeNull();
	});
});
