/**
 * Forward-only migration runner for Theo.
 *
 * Bootstrap sequence:
 * 1. Create _migrations table (IF NOT EXISTS) -- runner infrastructure, not a migration file.
 * 2. Discover .sql files from src/db/migrations/, sorted by name.
 * 3. Determine which are already applied.
 * 4. For each unapplied file: execute SQL + record in _migrations within a single transaction.
 * 5. Return applied count.
 *
 * Each migration runs in its own transaction. If a migration fails, its transaction
 * rolls back and subsequent migrations are not attempted.
 */

import { readdir } from "node:fs/promises";
import { resolve } from "node:path";
import type { Sql } from "postgres";
import type { AppError, Result } from "../errors.ts";
import { err, ok } from "../errors.ts";

/**
 * Discover .sql migration files from the migrations directory.
 * Returns filenames sorted lexicographically. Returns empty array if directory does not exist.
 */
async function listMigrationFiles(): Promise<readonly string[]> {
	const migrationsDir = resolve(import.meta.dir, "migrations");

	try {
		const entries = await readdir(migrationsDir, { withFileTypes: true });
		return entries
			.filter((entry) => entry.isFile() && entry.name.endsWith(".sql"))
			.map((entry) => entry.name)
			.sort();
	} catch (error: unknown) {
		if (error instanceof Error && "code" in error && error.code === "ENOENT") {
			return [];
		}
		throw error;
	}
}

/**
 * Run all pending migrations against the database.
 *
 * Creates the _migrations tracking table if it doesn't exist, then applies
 * each unapplied .sql file in its own transaction. Idempotent -- running
 * twice applies nothing the second time.
 */
async function migrate(sql: Sql): Promise<Result<{ applied: number }, AppError>> {
	// Step 1: Runner owns this table. Not a migration file.
	await sql`
		CREATE TABLE IF NOT EXISTS _migrations (
			name text PRIMARY KEY,
			applied_at timestamptz NOT NULL DEFAULT now()
		)
	`;

	// Step 2: Discover migration files
	const files = await listMigrationFiles();

	// Step 3: Determine which are already applied
	const applied = await sql`SELECT name FROM _migrations`;
	const appliedNames = new Set(applied.map((row) => String(row["name"])));

	// Step 4: Apply each new migration in its own transaction
	let count = 0;
	const migrationsDir = resolve(import.meta.dir, "migrations");

	for (const file of files) {
		if (appliedNames.has(file)) {
			continue;
		}

		const content = await Bun.file(`${migrationsDir}/${file}`).text();

		try {
			await sql.begin(async (tx) => {
				await tx.unsafe(content);
				// Use unsafe with parameterized args -- TransactionSql's Omit<Sql>
				// loses call signatures in TypeScript, but unsafe() is preserved.
				await tx.unsafe("INSERT INTO _migrations (name) VALUES ($1)", [file]);
			});
			count++;
		} catch (e: unknown) {
			return err({
				code: "MIGRATION_FAILED" as const,
				migration: file,
				message: e instanceof Error ? e.message : String(e),
			});
		}
	}

	return ok({ applied: count });
}

/**
 * CLI entrypoint: run migrations using config from environment.
 * Called directly via `bun run src/db/migrate.ts` / `just migrate`.
 */
async function main(): Promise<void> {
	const { loadConfig } = await import("../config.ts");
	const { createPool } = await import("./pool.ts");

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
		process.exit(1);
	}

	const migrateResult = await migrate(pool.sql);
	await pool.end();

	if (!migrateResult.ok) {
		const { error } = migrateResult;
		console.error(
			`Migration failed${error.code === "MIGRATION_FAILED" ? ` (${error.migration})` : ""}:`,
			error.message,
		);
		process.exit(1);
	}

	if (migrateResult.value.applied === 0) {
		console.info("No new migrations to apply.");
	} else {
		console.info(`Applied ${String(migrateResult.value.applied)} migration(s).`);
	}
}

// Only run main() when executed directly (not imported by tests)
const isDirectExecution = process.argv[1]?.endsWith("migrate.ts") ?? false;
if (isDirectExecution) {
	await main();
}

export { listMigrationFiles, migrate };
