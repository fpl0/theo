/**
 * Linear webhook parser.
 *
 * Linear sends webhooks with `linear-delivery` (delivery ID) and
 * `linear-signature` (HMAC-SHA256, hex) headers. The payload carries an
 * `action` field (create/update/remove) and a typed record.
 */

import type { ParsedWebhook } from "./types.ts";

export const LINEAR_SIGNATURE_HEADER = "linear-signature";
export const LINEAR_DELIVERY_HEADER = "linear-delivery";

interface LinearPayload {
	readonly action?: unknown;
	readonly type?: unknown;
	readonly data?: {
		readonly title?: unknown;
		readonly body?: unknown;
		readonly url?: unknown;
	};
}

export function parseLinearPayload(
	body: unknown,
	headers: Record<string, string | undefined>,
): ParsedWebhook | null {
	const deliveryId = headers[LINEAR_DELIVERY_HEADER];
	if (typeof deliveryId !== "string" || deliveryId.length === 0) return null;
	if (typeof body !== "object" || body === null) return null;
	const payload = body as LinearPayload;
	const action = typeof payload.action === "string" ? payload.action : "unknown";
	const type = typeof payload.type === "string" ? payload.type : "unknown";
	const title = typeof payload.data?.title === "string" ? payload.data.title : "";
	const text = typeof payload.data?.body === "string" ? payload.data.body.slice(0, 800) : "";

	const summary = `${type}.${action}${title !== "" ? `\nTitle: ${title}` : ""}${
		text !== "" ? `\nBody: ${text}` : ""
	}`;

	return {
		source: "linear",
		deliveryId,
		eventKind: `${type}.${action}`,
		summary,
		autonomyDomain: "issues.triage",
	};
}
