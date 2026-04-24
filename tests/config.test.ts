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

	test("error when DATABASE_URL missing", () => {
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
	});

	test("empty ANTHROPIC_API_KEY is coerced to unset (parent-shell inheritance)", () => {
		// Claude Code exports ANTHROPIC_API_KEY="" into every subprocess.
		// Treating empty as unset is the user's actual intent; rejecting it
		// would make the OAuth-token auth path unusable from that environment.
		const result = loadConfig({
			DATABASE_URL: "postgresql://u:p@localhost:5432/d",
			ANTHROPIC_API_KEY: "",
		});
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.value.ANTHROPIC_API_KEY).toBeUndefined();
	});

	test("whitespace-only ANTHROPIC_API_KEY is coerced to unset", () => {
		const result = loadConfig({
			DATABASE_URL: "postgresql://u:p@localhost:5432/d",
			ANTHROPIC_API_KEY: "   \t\n ",
		});
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.value.ANTHROPIC_API_KEY).toBeUndefined();
	});

	test("empty CLAUDE_CODE_OAUTH_TOKEN is coerced to unset", () => {
		const result = loadConfig({
			DATABASE_URL: "postgresql://u:p@localhost:5432/d",
			CLAUDE_CODE_OAUTH_TOKEN: "",
		});
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.value.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
	});

	test("unset ANTHROPIC_API_KEY is accepted (OAuth fallback)", () => {
		const result = loadConfig({
			DATABASE_URL: "postgresql://u:p@localhost:5432/d",
		});
		expect(result.ok).toBe(true);
	});
});
