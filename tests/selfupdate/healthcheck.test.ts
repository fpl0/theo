/**
 * Self-update health check — pass/fail paths, healthy-commit tracking.
 * No real shell execution; `runCheck`, `currentCommit`, `readHealthy`,
 * and `writeHealthy` are injected.
 */

import { describe, expect, test } from "bun:test";
import { runHealthCheck } from "../../src/selfupdate/healthcheck.ts";

function fakeDeps(overrides: Partial<Parameters<typeof runHealthCheck>[0]>) {
	const state: { healthy: string | null } = { healthy: overrides.readHealthy ? null : "old-sha" };
	return {
		workspace: "/tmp/theo-test",
		currentCommit: async () => "new-sha",
		readHealthy: async () => state.healthy,
		writeHealthy: async (commit: string) => {
			state.healthy = commit;
		},
		runCheck: async () => ({ ok: true, stderr: "" }),
		...overrides,
		stateRef: state,
	};
}

describe("runHealthCheck", () => {
	test("pass updates healthy_commit to current", async () => {
		const deps = fakeDeps({});
		const result = await runHealthCheck(deps);
		expect(result.ok).toBe(true);
		expect(result.commit).toBe("new-sha");
		expect(result.healthyCommit).toBe("new-sha");
		expect(deps.stateRef.healthy).toBe("new-sha");
	});

	test("fail leaves healthy_commit untouched", async () => {
		const deps = fakeDeps({
			runCheck: async () => ({ ok: false, stderr: "tsc: error" }),
		});
		const result = await runHealthCheck(deps);
		expect(result.ok).toBe(false);
		expect(result.commit).toBe("new-sha");
		expect(result.healthyCommit).toBe("old-sha"); // unchanged
		expect(result.errors).toEqual(["tsc: error"]);
		expect(deps.stateRef.healthy).toBe("old-sha");
	});

	test("idempotent — double run with no changes produces the same result", async () => {
		const deps = fakeDeps({});
		const a = await runHealthCheck(deps);
		const b = await runHealthCheck(deps);
		expect(a).toEqual(b);
	});
});
