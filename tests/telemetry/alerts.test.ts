/**
 * Alert rules — every alert carries a `runbook_url`, and the referenced
 * markdown file exists and includes the canonical four headings.
 */

import { describe, expect, test } from "bun:test";
import { readdir } from "node:fs/promises";
import * as path from "node:path";

const OPS = path.join(import.meta.dir, "..", "..", "ops", "observability");

describe("alerts + runbooks", () => {
	test("every alert has a runbook_url and the file exists with 4 headings", async () => {
		const rulesText = await Bun.file(
			path.join(OPS, "grafana", "provisioning", "alerting", "rules.yaml"),
		).text();
		// Minimal YAML parse: find every `runbook_url: "..."` and every `title: ...`.
		const runbookMatches = Array.from(rulesText.matchAll(/runbook_url:\s*"([^"]+)"/gu));
		const titleMatches = Array.from(rulesText.matchAll(/\btitle:\s*([A-Za-z0-9_]+)\b/gu));
		expect(runbookMatches.length).toBeGreaterThan(0);
		expect(titleMatches.length).toBe(runbookMatches.length);

		for (const match of runbookMatches) {
			const rel = match[1];
			if (rel === undefined) throw new Error("expected runbook_url capture group");
			// Relative path, under runbooks/
			expect(rel).toContain("runbooks/");
			const file = path.join(OPS, rel);
			const body = await Bun.file(file).text();
			for (const heading of ["## What it means", "## Triage", "## Resolution", "## Related"]) {
				expect(body).toContain(heading);
			}
		}
	});

	test("every runbook file on disk is referenced by at least one rule OR is a known catch-all", async () => {
		const runbooksDir = path.join(OPS, "runbooks");
		const files = await readdir(runbooksDir);
		const rulesText = await Bun.file(
			path.join(OPS, "grafana", "provisioning", "alerting", "rules.yaml"),
		).text();
		const unreferenced = files.filter((f) => !rulesText.includes(f));
		// For Phase 15 we don't create ghost runbooks. Assert zero unreferenced.
		expect(unreferenced).toEqual([]);
	});
});
