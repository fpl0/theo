/**
 * Dashboards as code — every dashboard JSON parses; every `expr` references
 * either a real metric instrument or a recording rule.
 */

import { describe, expect, test } from "bun:test";
import { readdir } from "node:fs/promises";
import * as path from "node:path";
import { ALL_METRIC_NAMES } from "../../src/telemetry/metrics.ts";

const DASH_DIR = path.join(
	import.meta.dir,
	"..",
	"..",
	"ops",
	"observability",
	"grafana",
	"dashboards",
);

function metricsFromExpr(expr: string): readonly string[] {
	// Extract identifier-like tokens that look like Prometheus metric names:
	// letters, digits, underscore, and colons (recording rules) — but not
	// function names immediately followed by '('. Simple regex suffices.
	const names: string[] = [];
	const tokenRe = /([a-zA-Z_:][a-zA-Z0-9_:]*)/gu;
	let match: RegExpExecArray | null;
	// biome-ignore lint/suspicious/noAssignInExpressions: regex loop idiom
	while ((match = tokenRe.exec(expr)) !== null) {
		const token = match[1];
		if (token === undefined) continue;
		// Skip if it's a PromQL function name (followed by `(`).
		const after = expr[match.index + token.length];
		if (after === "(") continue;
		// Skip standalone numbers, labels, operators, "bool" selector suffix.
		if (/^[0-9]+$/u.test(token)) continue;
		if (
			/^(by|le|status|gate|model|role|source|handler|job|service|le|bool|and|or|unless|without|on|ignoring|group_left|group_right|offset)$/u.test(
				token,
			)
		)
			continue;
		// Must look like a metric — either contains "theo_" or a ":"
		if (!token.includes("theo") && !token.includes(":")) continue;
		names.push(token);
	}
	return names;
}

function promMetricName(instrument: string): string {
	// OTel "theo.turns.total" → Prometheus "theo_turns_total".
	return instrument.replace(/\./g, "_");
}

describe("dashboards", () => {
	test("every dashboard JSON parses", async () => {
		const files = (await readdir(DASH_DIR)).filter((f) => f.endsWith(".json"));
		expect(files.length).toBeGreaterThan(0);
		for (const file of files) {
			const text = await Bun.file(path.join(DASH_DIR, file)).text();
			expect(() => JSON.parse(text)).not.toThrow();
		}
	});

	test("every expr references a known metric or recording rule", async () => {
		const allowed = new Set<string>([
			...ALL_METRIC_NAMES.map(promMetricName),
			// Recording rules from prometheus/recording_rules.yaml
			"theo:turns:p95_1h",
			"theo:turns:p99_1h",
			"theo:turns:error_rate_5m",
			"theo:retrieval:p95_5m",
			"theo:slo:turns_available_ratio_30d",
			"theo:slo:error_budget_remaining_ratio",
		]);
		const files = (await readdir(DASH_DIR)).filter((f) => f.endsWith(".json"));
		const drifted: Array<{ file: string; missing: string }> = [];
		for (const file of files) {
			const json = JSON.parse(await Bun.file(path.join(DASH_DIR, file)).text());
			const panels = (json.panels as Array<Record<string, unknown>>) ?? [];
			for (const panel of panels) {
				const targets = (panel["targets"] as Array<Record<string, unknown>>) ?? [];
				for (const t of targets) {
					const exprVal = t["expr"];
					const expr = typeof exprVal === "string" ? exprVal : "";
					for (const ref of metricsFromExpr(expr)) {
						// Strip bucket suffix for histograms.
						const stripped = ref.replace(/_bucket$/u, "");
						// Strip `_total` if the base is still recognized without it (some Prom
						// clients normalize that way). We keep both matches as valid.
						const noTotal = stripped.replace(/_total$/u, "");
						if (
							!allowed.has(ref) &&
							!allowed.has(stripped) &&
							!allowed.has(`${noTotal}_total`) &&
							!allowed.has(noTotal)
						) {
							drifted.push({ file, missing: ref });
						}
					}
				}
			}
		}
		expect(drifted).toEqual([]);
	});
});
