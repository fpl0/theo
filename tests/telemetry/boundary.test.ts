/**
 * Telemetry module boundary — enforce via a lint-over-source scan, since
 * Biome's `noRestrictedImports` doesn't cleanly express "this dir only"
 * under the current config.
 *
 * Business code MUST NOT import from `src/telemetry/*` directly. Only
 * `src/engine.ts` and `src/index.ts` may wire the bundle.
 */

import { describe, expect, test } from "bun:test";
import { readdir } from "node:fs/promises";
import * as path from "node:path";

const SRC = path.join(import.meta.dir, "..", "..", "src");

async function listSourceFiles(dir: string, out: string[] = []): Promise<string[]> {
	const entries = await readdir(dir, { withFileTypes: true });
	for (const e of entries) {
		const p = path.join(dir, e.name);
		if (e.isDirectory()) await listSourceFiles(p, out);
		else if (e.isFile() && (e.name.endsWith(".ts") || e.name.endsWith(".tsx"))) out.push(p);
	}
	return out;
}

describe("telemetry boundary", () => {
	test("no business file imports from src/telemetry/*", async () => {
		const files = await listSourceFiles(SRC);
		const offenders: Array<{ file: string; line: string }> = [];
		for (const file of files) {
			const rel = path.relative(SRC, file).replace(/\\/gu, "/");
			// Allowed: telemetry module itself, engine.ts, index.ts, tests.
			if (rel.startsWith("telemetry/")) continue;
			if (rel === "engine.ts" || rel === "index.ts") continue;
			const text = await Bun.file(file).text();
			const lines = text.split(/\n/u);
			for (const line of lines) {
				// Typescript side-import only matters; we look for `from ".../telemetry/..."`.
				if (
					/from\s+["'][^"']*\.\/telemetry\//u.test(line) ||
					/from\s+["'][^"']*\/telemetry\//u.test(line)
				) {
					offenders.push({ file: rel, line });
				}
			}
		}
		expect(offenders).toEqual([]);
	});
});
