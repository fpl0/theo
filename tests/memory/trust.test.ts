/**
 * Trust walker tests — `src/memory/trust.ts`.
 *
 * The walker is pure except for a single SELECT on the events table per
 * ancestor. We use an in-memory stub for `sql` that returns canned rows
 * keyed by event id, so the tests are deterministic without a real DB.
 */

import { describe, expect, test } from "bun:test";
import type { TrustTier } from "../../src/memory/graph/types.ts";
import {
	actorTrust,
	computeEffectiveTrust,
	isExternalTier,
	MAX_CAUSATION_DEPTH,
	minTier,
	weakerThan,
} from "../../src/memory/trust.ts";

interface StubRow {
	readonly id: string;
	readonly tier: TrustTier;
	readonly causeId: string | null;
}

/** Build a tagged-template-callable stub that returns rows matching ids. */
function stubSql(rows: readonly StubRow[]): {
	tag: (strings: TemplateStringsArray, ...values: unknown[]) => Promise<unknown[]>;
} {
	const byId = new Map(rows.map((r) => [r.id, r]));
	const tag = async (_strings: TemplateStringsArray, ...values: unknown[]): Promise<unknown[]> => {
		// The walker passes the event id as the only parameter.
		const id = String(values[0] ?? "");
		const row = byId.get(id);
		if (!row) return [];
		return [
			{
				["effective_trust_tier"]: row.tier,
				["cause_id"]: row.causeId,
			},
		];
	};
	return { tag };
}

describe("minTier", () => {
	test("owner vs external picks external", () => {
		expect(minTier("owner", "external")).toBe("external");
	});
	test("inferred vs untrusted picks untrusted", () => {
		expect(minTier("inferred", "untrusted")).toBe("untrusted");
	});
	test("commutativity", () => {
		expect(minTier("verified", "inferred")).toBe(minTier("inferred", "verified"));
	});
});

describe("actorTrust", () => {
	test("user is owner", () => {
		expect(actorTrust("user")).toBe("owner");
	});
	test("theo is owner_confirmed", () => {
		expect(actorTrust("theo")).toBe("owner_confirmed");
	});
	test("scheduler is owner_confirmed", () => {
		expect(actorTrust("scheduler")).toBe("owner_confirmed");
	});
});

describe("weakerThan", () => {
	test("weaker than owner returns all other tiers", () => {
		const list = weakerThan("owner");
		expect(list).toContain("external");
		expect(list).toContain("untrusted");
		expect(list).not.toContain("owner");
	});
	test("weaker than untrusted is empty", () => {
		expect(weakerThan("untrusted")).toEqual([]);
	});
});

describe("isExternalTier", () => {
	test("external is external", () => {
		expect(isExternalTier("external")).toBe(true);
	});
	test("untrusted is external", () => {
		expect(isExternalTier("untrusted")).toBe(true);
	});
	test("owner is not external", () => {
		expect(isExternalTier("owner")).toBe(false);
	});
});

describe("computeEffectiveTrust", () => {
	test("owner event without cause → owner", async () => {
		const stub = stubSql([]);
		const tier = await computeEffectiveTrust(stub.tag as never, "user", {});
		expect(tier).toBe("owner");
	});

	test("override returns the override unconditionally", async () => {
		const stub = stubSql([]);
		const tier = await computeEffectiveTrust(
			stub.tag as never,
			"user",
			{ causeId: "anything" as never },
			{ override: "external" },
		);
		expect(tier).toBe("external");
	});

	test("seedTier overrides actor tier", async () => {
		const stub = stubSql([]);
		const tier = await computeEffectiveTrust(
			stub.tag as never,
			"user",
			{},
			{ seedTier: "external" },
		);
		expect(tier).toBe("external");
	});

	test("user event caused by external webhook → external", async () => {
		const stub = stubSql([{ id: "W1", tier: "external", causeId: null }]);
		const tier = await computeEffectiveTrust(stub.tag as never, "user", { causeId: "W1" as never });
		expect(tier).toBe("external");
	});

	test("theo event caused by user event → owner_confirmed (minimum)", async () => {
		const stub = stubSql([{ id: "U1", tier: "owner", causeId: null }]);
		const tier = await computeEffectiveTrust(stub.tag as never, "theo", { causeId: "U1" as never });
		// theo actor = owner_confirmed; user parent = owner. min = owner_confirmed.
		expect(tier).toBe("owner_confirmed");
	});

	test("causation depth cap forces external when exceeded", async () => {
		// Build a cause chain longer than MAX_CAUSATION_DEPTH with all owner tiers.
		const rows: StubRow[] = [];
		for (let i = 0; i < MAX_CAUSATION_DEPTH + 2; i++) {
			rows.push({
				id: `E${i}`,
				tier: "owner",
				causeId: `E${i + 1}`,
			});
		}
		const stub = stubSql(rows);
		const tier = await computeEffectiveTrust(stub.tag as never, "user", { causeId: "E0" as never });
		expect(tier).toBe("external");
	});

	test("chain collapses to the first external ancestor", async () => {
		const stub = stubSql([
			{ id: "A", tier: "owner_confirmed", causeId: "B" },
			{ id: "B", tier: "external", causeId: null },
		]);
		const tier = await computeEffectiveTrust(stub.tag as never, "theo", { causeId: "A" as never });
		expect(tier).toBe("external");
	});
});
