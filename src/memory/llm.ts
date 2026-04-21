/**
 * Shared one-shot LLM helper for background-intelligence handlers
 * (contradiction classification, pattern synthesis, episode summarization).
 *
 * Each call is a single turn with no tools and no session, isolated from
 * settings and operator state. Structured-output callers get typed parsed
 * results; free-form callers get trimmed text.
 */

import { query as sdkQuery } from "@anthropic-ai/claude-agent-sdk";

export interface CheapQueryOptions {
	readonly prompt: string;
	readonly schema?: Record<string, unknown>;
}

export interface CheapQueryResult {
	readonly text: string | null;
	readonly structured: unknown;
}

export async function cheapQuery(opts: CheapQueryOptions): Promise<CheapQueryResult> {
	const outputFormat =
		opts.schema === undefined ? undefined : { type: "json_schema" as const, schema: opts.schema };

	const generator = sdkQuery({
		prompt: opts.prompt,
		options: {
			model: "haiku",
			settingSources: [],
			permissionMode: "bypassPermissions",
			allowDangerouslySkipPermissions: true,
			maxTurns: 1,
			persistSession: false,
			allowedTools: [],
			...(outputFormat === undefined ? {} : { outputFormat }),
		},
	});

	for await (const message of generator) {
		if (message.type !== "result" || message.subtype !== "success") continue;
		return {
			text: message.result.trim(),
			structured: message.structured_output,
		};
	}
	return { text: null, structured: undefined };
}
