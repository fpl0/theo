/**
 * Public-bind safety regression — the webhook server refuses to bind
 * publicly without both a tunnel AND an explicit, non-0.0.0.0 hostname.
 */

import { describe, expect, test } from "bun:test";
import { startWebhookServer } from "../../../src/gates/webhooks/server.ts";

const fakeDeps = (cfg: Parameters<typeof startWebhookServer>[0]["config"]) =>
	({
		sql: null as never,
		bus: null as never,
		secrets: null as never,
		rateLimiter: null as never,
		config: cfg,
	}) as Parameters<typeof startWebhookServer>[0];

describe("webhook bind safety", () => {
	test("public without tunnel refuses to start", () => {
		expect(() =>
			startWebhookServer(
				fakeDeps({
					port: 0,
					sources: [],
					public: true,
				}),
			),
		).toThrow(/without a tunnel/u);
	});

	test("public with tunnel but no hostname refuses to start", () => {
		expect(() =>
			startWebhookServer(
				fakeDeps({
					port: 0,
					sources: [],
					public: true,
					tunnel: "tailscale",
				}),
			),
		).toThrow(/without an explicit hostname/u);
	});

	test("public with hostname '0.0.0.0' refuses to start", () => {
		expect(() =>
			startWebhookServer(
				fakeDeps({
					port: 0,
					sources: [],
					public: true,
					tunnel: "tailscale",
					hostname: "0.0.0.0",
				}),
			),
		).toThrow(/refuses hostname '0\.0\.0\.0'/u);
	});
});
