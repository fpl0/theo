import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

export function errorResult(error: unknown): CallToolResult {
	const message = error instanceof Error ? error.message : String(error);
	return {
		content: [{ type: "text", text: `Error: ${message}` }],
		isError: true,
	};
}
