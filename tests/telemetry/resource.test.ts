/**
 * Resource attribute tests — confirm required semconv keys are present and
 * that the git sha override lands on `service.version`.
 */

import { describe, expect, test } from "bun:test";
import { buildResource } from "../../src/telemetry/resource.ts";
import {
	ATTR_DEPLOYMENT_ENVIRONMENT,
	ATTR_HOST_ARCH,
	ATTR_HOST_OS_TYPE,
	ATTR_PROCESS_RUNTIME_NAME,
	ATTR_PROCESS_RUNTIME_VERSION,
	ATTR_SERVICE_INSTANCE_ID,
	ATTR_SERVICE_NAME,
	ATTR_SERVICE_VERSION,
} from "../../src/telemetry/semconv.ts";

describe("buildResource", () => {
	test("emits every required attribute", async () => {
		const r = await buildResource({
			environment: "test",
			gitSha: "deadbeef",
			instanceId: "test-host",
		});
		expect(r[ATTR_SERVICE_NAME]).toBe("theo");
		expect(r[ATTR_SERVICE_VERSION]).toBe("deadbeef");
		expect(r[ATTR_SERVICE_INSTANCE_ID]).toBe("test-host");
		expect(r[ATTR_DEPLOYMENT_ENVIRONMENT]).toBe("test");
		expect(r[ATTR_PROCESS_RUNTIME_NAME]).toBe("bun");
		expect(r[ATTR_PROCESS_RUNTIME_VERSION]).toBe(Bun.version);
		expect(r[ATTR_HOST_OS_TYPE]).toBe(process.platform);
		expect(r[ATTR_HOST_ARCH]).toBe(process.arch);
	});
});
