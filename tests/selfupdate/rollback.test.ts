/**
 * Self-update rollback — resets to healthy_commit and emits
 * `system.rollback`. Throws when no healthy commit is recorded.
 */

import { describe, expect, test } from "bun:test";
import type { EventBus } from "../../src/events/bus.ts";
import { rollbackToHealthy } from "../../src/selfupdate/rollback.ts";

function makeBus(): {
	bus: EventBus;
	emitted: Array<{ type: string; data: unknown }>;
} {
	const emitted: Array<{ type: string; data: unknown }> = [];
	const bus = {
		emit: async (event: { type: string; data: unknown }) => {
			emitted.push({ type: event.type, data: event.data });
			return { id: "x", timestamp: new Date(), ...event } as never;
		},
	} as unknown as EventBus;
	return { bus, emitted };
}

describe("rollbackToHealthy", () => {
	test("resets to healthy commit and emits system.rollback", async () => {
		const { bus, emitted } = makeBus();
		const resets: string[] = [];
		const result = await rollbackToHealthy({
			workspace: "/tmp/theo-test",
			bus,
			currentCommit: async () => "bad-sha",
			readHealthy: async () => "good-sha",
			gitReset: async (commit: string) => {
				resets.push(commit);
			},
		});
		expect(result).toEqual({ from: "bad-sha", to: "good-sha" });
		expect(resets).toEqual(["good-sha"]);
		expect(emitted).toHaveLength(1);
		expect(emitted[0]?.type).toBe("system.rollback");
		expect(emitted[0]?.data).toMatchObject({
			fromCommit: "bad-sha",
			toCommit: "good-sha",
			reason: "healthcheck_failed",
		});
	});

	test("throws when no healthy commit is recorded", async () => {
		const { bus } = makeBus();
		let thrown: unknown = null;
		try {
			await rollbackToHealthy({
				workspace: "/tmp/theo-test",
				bus,
				currentCommit: async () => "bad-sha",
				readHealthy: async () => null,
				gitReset: async () => {},
			});
		} catch (err) {
			thrown = err;
		}
		expect(thrown).toBeInstanceOf(Error);
		expect((thrown as Error).message).toContain("no healthy commit");
	});

	test("does not emit when git reset fails", async () => {
		const { bus, emitted } = makeBus();
		let thrown: unknown = null;
		try {
			await rollbackToHealthy({
				workspace: "/tmp/theo-test",
				bus,
				currentCommit: async () => "bad-sha",
				readHealthy: async () => "good-sha",
				gitReset: async () => {
					throw new Error("reset failed");
				},
			});
		} catch (err) {
			thrown = err;
		}
		expect(thrown).toBeInstanceOf(Error);
		expect(emitted).toHaveLength(0);
	});
});
