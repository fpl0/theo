/**
 * Zod-validated environment configuration for Theo.
 *
 * Not a singleton -- tests construct config objects directly via the exported schema.
 * loadConfig() reads from process.env by default but accepts any Record for testing.
 */

import { z } from "zod/v4";
import type { AppError, Result } from "./errors.ts";
import { err, ok } from "./errors.ts";

/** Default maximum connections in the pool. */
const DEFAULT_POOL_MAX = 10;

/** Default idle timeout in seconds before a connection is closed. */
const DEFAULT_IDLE_TIMEOUT = 30;

/** Default connection timeout in seconds. */
const DEFAULT_CONNECT_TIMEOUT = 10;

/** Schema for environment-based configuration. Exported for direct use in tests. */
export const configSchema = z.object({
	// Required
	DATABASE_URL: z.url(),
	ANTHROPIC_API_KEY: z.string().min(1),

	// Optional with defaults -- pool tuning
	DB_POOL_MAX: z.coerce.number().default(DEFAULT_POOL_MAX),
	DB_IDLE_TIMEOUT: z.coerce.number().default(DEFAULT_IDLE_TIMEOUT),
	DB_CONNECT_TIMEOUT: z.coerce.number().default(DEFAULT_CONNECT_TIMEOUT),

	// Optional -- gates not required at startup
	TELEGRAM_BOT_TOKEN: z.string().optional(),
	TELEGRAM_OWNER_ID: z.string().optional(),
});

/** Validated configuration object. */
export type Config = z.infer<typeof configSchema>;

/**
 * Subset of Config relevant to database connectivity.
 * Used by createPool() to avoid coupling to the full config shape.
 */
export type DbConfig = Pick<
	Config,
	"DATABASE_URL" | "DB_POOL_MAX" | "DB_IDLE_TIMEOUT" | "DB_CONNECT_TIMEOUT"
>;

/**
 * Parse and validate configuration from environment variables.
 *
 * Returns Result -- never throws. On failure, each Zod issue is mapped to
 * a { path, message } pair in the CONFIG_INVALID error.
 *
 * @param env - Environment record to parse. Defaults to process.env.
 */
export function loadConfig(
	env: Record<string, string | undefined> = process.env,
): Result<Config, AppError> {
	const parsed = configSchema.safeParse(env);
	if (parsed.success) {
		return ok(parsed.data);
	}
	return err({
		code: "CONFIG_INVALID" as const,
		message: "Invalid configuration",
		issues: parsed.error.issues.map((issue) => ({
			path: issue.path.map(String).join("."),
			message: issue.message,
		})),
	});
}
