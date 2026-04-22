/**
 * Onboarding detection and interview prompt.
 */

import { describe, expect, test } from "bun:test";
import {
	augmentSystemPromptForOnboarding,
	ONBOARDING_INTERVIEW_PROMPT,
	shouldOnboard,
} from "../../src/chat/onboarding.ts";
import type { JsonValue } from "../../src/memory/types.ts";
import type { UserModelDimension, UserModelRepository } from "../../src/memory/user_model.ts";

function stubUserModel(dimensions: readonly UserModelDimension[]): UserModelRepository {
	return {
		async getDimensions() {
			return dimensions;
		},
		async getDimension() {
			return null;
		},
		async updateDimension(): Promise<UserModelDimension> {
			throw new Error("updateDimension should not be called");
		},
	};
}

function makeDim(name: string, value: JsonValue): UserModelDimension {
	const now = new Date();
	return {
		id: 1,
		name,
		value,
		confidence: 0.5,
		evidenceCount: 1,
		threshold: 10,
		egressSensitivity: "private",
		createdAt: now,
		updatedAt: now,
	};
}

describe("shouldOnboard", () => {
	test("returns true when no user-model dimensions exist", async () => {
		const repo = stubUserModel([]);
		expect(await shouldOnboard(repo)).toBe(true);
	});

	test("returns false when at least one dimension exists", async () => {
		const repo = stubUserModel([makeDim("communication_style", "direct")]);
		expect(await shouldOnboard(repo)).toBe(false);
	});

	test("returns false when many dimensions exist", async () => {
		const repo = stubUserModel([
			makeDim("communication_style", "direct"),
			makeDim("energy_patterns", "morning"),
			makeDim("boundaries", "polite"),
		]);
		expect(await shouldOnboard(repo)).toBe(false);
	});
});

describe("ONBOARDING_INTERVIEW_PROMPT", () => {
	test("contains all three phase markers", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("PHASE 1");
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("PHASE 2");
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("PHASE 3");
	});

	test("names the psychologist subagent", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("psychologist");
	});

	test("references the MCP memory tools used during the interview", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("store_memory");
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("update_user_model");
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("update_core");
	});

	test("phase 1 is labelled NARRATIVE and mentions stories", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("NARRATIVE");
		expect(ONBOARDING_INTERVIEW_PROMPT.toLowerCase()).toContain("stories");
	});

	test("phase 2 targets structured dimensions explicitly", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("STRUCTURED DIMENSIONS");
		expect(ONBOARDING_INTERVIEW_PROMPT.toLowerCase()).toContain("communication style");
		expect(ONBOARDING_INTERVIEW_PROMPT.toLowerCase()).toContain("energy patterns");
	});

	test("phase 3 covers the working agreement", () => {
		expect(ONBOARDING_INTERVIEW_PROMPT).toContain("WORKING AGREEMENT");
		expect(ONBOARDING_INTERVIEW_PROMPT.toLowerCase()).toContain("autonom");
	});
});

describe("onboarding prompt augmentation", () => {
	test("prepends the interview prompt to the base prompt", () => {
		const base = "You are Theo.";
		const result = augmentSystemPromptForOnboarding(base);
		expect(result.startsWith(ONBOARDING_INTERVIEW_PROMPT)).toBe(true);
		expect(result.endsWith(base)).toBe(true);
	});

	test("the base prompt is separated by a blank line", () => {
		const base = "You are Theo.";
		const result = augmentSystemPromptForOnboarding(base);
		expect(result).toContain(`${ONBOARDING_INTERVIEW_PROMPT}\n\n${base}`);
	});
});
