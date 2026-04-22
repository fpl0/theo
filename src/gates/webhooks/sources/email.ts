/**
 * Inbound email parser (via trusted relay).
 *
 * Theo does not accept raw SMTP — inbound email arrives via a trusted relay
 * that the owner runs, which signs every forwarded message with a
 * Theo-managed HMAC secret. The relay forwards a JSON envelope with
 * `messageId`, `from`, `subject`, and `bodyText`. Raw email parsing is out
 * of scope.
 */

import type { ParsedWebhook } from "./types.ts";

export const EMAIL_SIGNATURE_HEADER = "x-theo-relay-signature";
export const EMAIL_DELIVERY_HEADER = "x-theo-relay-delivery";

interface EmailPayload {
	readonly messageId?: unknown;
	readonly from?: unknown;
	readonly subject?: unknown;
	readonly bodyText?: unknown;
}

export function parseEmailPayload(
	body: unknown,
	headers: Record<string, string | undefined>,
): ParsedWebhook | null {
	const deliveryId =
		headers[EMAIL_DELIVERY_HEADER] ??
		(typeof (body as EmailPayload | null)?.messageId === "string"
			? String((body as EmailPayload).messageId)
			: undefined);
	if (typeof deliveryId !== "string" || deliveryId.length === 0) return null;
	if (typeof body !== "object" || body === null) return null;
	const payload = body as EmailPayload;
	const from = typeof payload.from === "string" ? payload.from : "unknown@unknown";
	const subject = typeof payload.subject === "string" ? payload.subject : "(no subject)";
	const text = typeof payload.bodyText === "string" ? payload.bodyText.slice(0, 2000) : "";

	const summary = `From: ${from}\nSubject: ${subject}\n\n${text}`;

	return {
		source: "email",
		deliveryId,
		eventKind: "email.received",
		summary,
		autonomyDomain: "messaging.draft",
	};
}
