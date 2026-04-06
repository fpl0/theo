import { readdir } from "node:fs/promises";

async function listMigrationFiles(): Promise<readonly string[]> {
	const migrationsDirectory = new URL("./migrations/", import.meta.url);

	try {
		const entries = await readdir(migrationsDirectory, { withFileTypes: true });

		return entries
			.filter((entry) => entry.isFile() && entry.name.endsWith(".sql"))
			.map((entry) => entry.name)
			.sort();
	} catch (error) {
		if (error instanceof Error && "code" in error && error.code === "ENOENT") {
			return [];
		}

		throw error;
	}
}

const migrationFiles = await listMigrationFiles();

if (migrationFiles.length === 0) {
	console.info("No migrations found in src/db/migrations; nothing to apply.");
	process.exit(0);
}

console.info("Discovered migration files:");
for (const migrationFile of migrationFiles) {
	console.info(`- ${migrationFile}`);
}

console.info(
	"Migration execution is not implemented yet. Add the database layer before applying SQL files.",
);
process.exit(0);
