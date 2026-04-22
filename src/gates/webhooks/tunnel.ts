/**
 * Optional tunnel adapter stub.
 *
 * Phase 13b does not ship a concrete tunnel implementation — the owner is
 * expected to provision a Cloudflare Tunnel, Tailscale funnel, or a trusted
 * relay outside of Theo. This module exists so the config can declare which
 * tunnel is in use for logging + refusal logic without embedding
 * credentials or orchestration code inside Theo.
 *
 * Phase 15 may add a `cloudflared`-launcher if the operational burden
 * proves worthwhile; foundation keeps the surface minimal.
 */

import type { WebhookTunnel } from "./server.ts";

export interface TunnelInfo {
	readonly kind: WebhookTunnel;
	readonly note: string;
}

/**
 * Produce a human-readable description of how to connect the tunnel. The
 * startup path logs this so the owner sees what is expected.
 */
export function describeTunnel(kind: WebhookTunnel): TunnelInfo {
	switch (kind) {
		case "cloudflare":
			return {
				kind,
				note: "Start Cloudflare Tunnel with `cloudflared tunnel --url http://127.0.0.1:PORT` and map the hostname in your CF dashboard.",
			};
		case "tailscale":
			return {
				kind,
				note: "Enable Tailscale funnel: `tailscale funnel PORT` and confirm ACLs.",
			};
		case "relay":
			return {
				kind,
				note: "Configure the owner-managed relay to forward signed webhooks to http://127.0.0.1:PORT/:source",
			};
	}
}
