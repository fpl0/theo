/**
 * Shared shapes for webhook source parsers.
 */

export interface ParsedWebhook {
	readonly source: string;
	readonly deliveryId: string;
	readonly eventKind: string;
	/** Safe, length-bounded summary for the reflex turn. */
	readonly summary: string;
	/** Autonomy domain hint for the autonomy-ladder gate. */
	readonly autonomyDomain: string;
}

/** Parsers return null when the body does not satisfy the minimum shape. */
export type Parser = (
	body: unknown,
	headers: Record<string, string | undefined>,
) => ParsedWebhook | null;
