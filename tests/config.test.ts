import { describe, expect, test } from "bun:test";
import { loadConfig } from "../src/config.ts";
import { testDbConfig } from "./helpers.ts";

/** Minimal valid environment for config parsing. */
const validEnv = {
	DATABASE_URL: testDbConfig.DATABASE_URL,
	ANTHROPIC_API_KEY: "sk-ant-test-key",
};

describe("loadConfig valid input", () => {
	test("returns ok with correct types", () => {
		const result = loadConfig(validEnv);
		expect(result.ok).toBe(true);
		if (!result.ok) {
			return;
		}

		expect(result.value.DATABASE_URL).toBe(validEnv.DATABASE_URL);
		expect(result.value.ANTHROPIC_API_KEY).toBe(validEnv.ANTHROPIC_API_KEY);
	});

	test("defaults applied when only required vars present", () => {
		const result = loadConfig(validEnv);
		expect(result.ok).toBe(true);
		if (!result.ok) {
			return;
		}

		expect(result.value.DB_POOL_MAX).toBe(10);
		expect(result.value.DB_IDLE_TIMEOUT).toBe(30);
		expect(result.value.DB_CONNECT_TIMEOUT).toBe(10);
	});

	test("optional fields absent results in undefined", () => {
		const result = loadConfig(validEnv);
		expect(result.ok).toBe(true);
		if (!result.ok) {
			return;
		}

		expect(result.value.TELEGRAM_BOT_TOKEN).toBeUndefined();
		expect(result.value.TELEGRAM_OWNER_ID).toBeUndefined();
	});
});

describe("loadConfig invalid input", () => {
	test("missing DATABASE_URL returns CONFIG_INVALID with issues", () => {
		const result = loadConfig({ ANTHROPIC_API_KEY: "sk-ant-test-key" });
		expect(result.ok).toBe(false);
		if (result.ok) {
			return;
		}

		expect(result.error.code).toBe("CONFIG_INVALID");
		if (result.error.code !== "CONFIG_INVALID") {
			return;
		}

		const dbIssue = result.error.issues.find((i) => i.path === "DATABASE_URL");
		expect(dbIssue).toBeDefined();
	});

	test("invalid DATABASE_URL returns CONFIG_INVALID", () => {
		const result = loadConfig({
			DATABASE_URL: "not-a-url",
			ANTHROPIC_API_KEY: "sk-ant-test-key",
		});
		expect(result.ok).toBe(false);
		if (result.ok) {
			return;
		}

		expect(result.error.code).toBe("CONFIG_INVALID");
	});

	test("multiple errors when both required vars missing", () => {
		const result = loadConfig({});
		expect(result.ok).toBe(false);
		if (result.ok) {
			return;
		}

		expect(result.error.code).toBe("CONFIG_INVALID");
		if (result.error.code !== "CONFIG_INVALID") {
			return;
		}

		const paths = result.error.issues.map((i) => i.path);
		expect(paths).toContain("DATABASE_URL");
		expect(paths).toContain("ANTHROPIC_API_KEY");
	});
});
