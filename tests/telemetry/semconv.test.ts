/**
 * Semconv discipline — keys used by the telemetry module use OTel standard
 * names where a convention exists, and the Theo-specific extensions live
 * under the `theo.*` namespace only.
 */

import { describe, expect, test } from "bun:test";
import * as Sem from "../../src/telemetry/semconv.ts";

describe("semantic conventions", () => {
	test("db / messaging / code / service keys match OTel standard strings", () => {
		expect(Sem.ATTR_DB_SYSTEM).toBe("db.system");
		expect(Sem.ATTR_DB_OPERATION).toBe("db.operation");
		expect(Sem.ATTR_DB_STATEMENT).toBe("db.statement");
		expect(Sem.ATTR_MESSAGING_SYSTEM).toBe("messaging.system");
		expect(Sem.ATTR_MESSAGING_DESTINATION).toBe("messaging.destination.name");
		expect(Sem.ATTR_MESSAGING_OP).toBe("messaging.operation");
		expect(Sem.ATTR_CODE_FUNCTION).toBe("code.function");
		expect(Sem.ATTR_CODE_NAMESPACE).toBe("code.namespace");
		expect(Sem.ATTR_SERVICE_NAME).toBe("service.name");
		expect(Sem.ATTR_SERVICE_VERSION).toBe("service.version");
		expect(Sem.ATTR_SERVICE_INSTANCE_ID).toBe("service.instance.id");
		expect(Sem.ATTR_DEPLOYMENT_ENVIRONMENT).toBe("deployment.environment");
	});

	test("theo-specific attributes live only under theo.*", () => {
		const keys = [
			Sem.ATTR_THEO_GATE,
			Sem.ATTR_THEO_MODEL,
			Sem.ATTR_THEO_ROLE,
			Sem.ATTR_THEO_GOAL_ID,
			Sem.ATTR_THEO_PROPOSAL_ID,
			Sem.ATTR_THEO_TURN_CLASS,
			Sem.ATTR_THEO_EVENT_ID,
			Sem.ATTR_THEO_EVENT_TYPE,
			Sem.ATTR_THEO_EVENT_VERSION,
			Sem.ATTR_THEO_MESSAGE_LENGTH,
			Sem.ATTR_THEO_AUTONOMY_DOMAIN,
			Sem.ATTR_THEO_DEGRADATION_LEVEL,
		];
		for (const k of keys) expect(k.startsWith("theo.")).toBe(true);
	});

	test("ALL_ATTRIBUTE_KEYS is unique and non-empty", () => {
		const set = new Set(Sem.ALL_ATTRIBUTE_KEYS);
		expect(set.size).toBe(Sem.ALL_ATTRIBUTE_KEYS.length);
		expect(Sem.ALL_ATTRIBUTE_KEYS.length).toBeGreaterThan(20);
	});

	test("MESSAGING_SYSTEM_THEO identifies the bus as 'theo.eventbus'", () => {
		expect(Sem.MESSAGING_SYSTEM_THEO).toBe("theo.eventbus");
	});
});
