/**
 * GitHub webhook parser.
 *
 * GitHub sends webhooks with a `x-github-delivery` header (unique delivery
 * ID, used for dedup) and `x-hub-signature-256` header (HMAC-SHA256 of the
 * body, hex-encoded, prefixed with `sha256=`). The payload body is JSON.
 *
 * This module is parser-only — signature verification is in `signature.ts`.
 * We extract the delivery id, a safe content excerpt, and an autonomy
 * domain hint for the reflex handler.
 */

import type { ParsedWebhook } from "./types.ts";

export const GITHUB_SIGNATURE_HEADER = "x-hub-signature-256";
export const GITHUB_DELIVERY_HEADER = "x-github-delivery";
export const GITHUB_EVENT_HEADER = "x-github-event";

/**
 * Minimal shape for a GitHub webhook payload. Field names match the API
 * literal keys (snake_case) to keep the mapping obvious. We access them
 * through bracket notation so the project's camelCase rule stays honored.
 */
type GithubPayload = Record<string, unknown>;

export function parseGithubPayload(
	body: unknown,
	headers: Record<string, string | undefined>,
): ParsedWebhook | null {
	const deliveryId = headers[GITHUB_DELIVERY_HEADER];
	if (typeof deliveryId !== "string" || deliveryId.length === 0) return null;
	const event = headers[GITHUB_EVENT_HEADER] ?? "unknown";
	if (typeof body !== "object" || body === null) return null;
	const payload = body as GithubPayload;

	const action = typeof payload["action"] === "string" ? (payload["action"] as string) : "";
	const repository = payload["repository"] as Record<string, unknown> | undefined;
	const repo =
		typeof repository?.["full_name"] === "string" ? (repository["full_name"] as string) : "";
	const sender = payload["sender"] as Record<string, unknown> | undefined;
	const senderLogin = typeof sender?.["login"] === "string" ? (sender["login"] as string) : "";

	// Extract a short, safe summary based on the event type.
	let summary = `${event}${action !== "" ? `.${action}` : ""} in ${repo}`;
	const pr = payload["pull_request"] as Record<string, unknown> | undefined;
	if (typeof pr?.["title"] === "string") {
		summary += `\nPR: ${pr["title"] as string}`;
	}
	const issue = payload["issue"] as Record<string, unknown> | undefined;
	if (typeof issue?.["title"] === "string") {
		summary += `\nIssue: ${issue["title"] as string}`;
	}
	const comment = payload["comment"] as Record<string, unknown> | undefined;
	if (typeof comment?.["body"] === "string") {
		// Comments are the highest injection-risk surface — cap length tightly.
		summary += `\nComment (${senderLogin}): ${(comment["body"] as string).slice(0, 800)}`;
	}

	return {
		source: "github",
		deliveryId,
		eventKind: event,
		summary,
		autonomyDomain: eventToDomain(event),
	};
}

function eventToDomain(event: string): string {
	switch (event) {
		case "pull_request":
		case "pull_request_review":
		case "pull_request_review_comment":
		case "push":
			return "code.review";
		case "issues":
		case "issue_comment":
			return "issues.triage";
		default:
			return "github.generic";
	}
}
