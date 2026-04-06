/**
 * Theo runtime entrypoint.
 *
 * Loads configuration, establishes database connectivity, runs migrations,
 * then starts the agent. Currently minimal -- grows with each phase.
 */

import { loadConfig } from "./config.ts";
import { migrate } from "./db/migrate.ts";
import { createPool } from "./db/pool.ts";

const configResult = loadConfig();
if (!configResult.ok) {
	const { error } = configResult;
	console.error("Configuration error:", error.message);
	if (error.code === "CONFIG_INVALID") {
		for (const issue of error.issues) {
			console.error(`  ${issue.path}: ${issue.message}`);
		}
	}
	process.exit(1);
}

const pool = createPool(configResult.value);

const connectResult = await pool.connect();
if (!connectResult.ok) {
	console.error("Database connection failed:", connectResult.error.message);
	await pool.end();
	process.exit(1);
}

console.info("Connected to PostgreSQL.");

const migrateResult = await migrate(pool.sql);
if (!migrateResult.ok) {
	const { error } = migrateResult;
	console.error(
		`Migration failed${error.code === "MIGRATION_FAILED" ? ` (${error.migration})` : ""}:`,
		error.message,
	);
	await pool.end();
	process.exit(1);
}

if (migrateResult.value.applied > 0) {
	console.info(`Applied ${String(migrateResult.value.applied)} migration(s).`);
}

console.info("Theo is ready.");

// Clean shutdown on SIGINT/SIGTERM
process.on("SIGINT", async () => {
	console.info("Shutting down...");
	await pool.end();
	process.exit(0);
});

process.on("SIGTERM", async () => {
	console.info("Shutting down...");
	await pool.end();
	process.exit(0);
});
