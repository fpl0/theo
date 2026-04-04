import { expect, test } from "bun:test";

test("bun runtime is operational", () => {
	expect(Bun.version).toBeTruthy();
});
