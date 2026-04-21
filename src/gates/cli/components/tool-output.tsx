/**
 * Render inline status for the tool calls triggered during a turn.
 *
 * Each row is one call: a spinner while the engine is waiting on the tool, a
 * green checkmark with duration once `tool.done` arrives. The component has
 * no local state — it is driven purely by props so the parent can rebuild the
 * list from ephemeral events.
 */

import { Spinner } from "@inkjs/ui";
import { Box, Text } from "ink";
import type React from "react";
import type { ToolCall } from "../state.ts";
import { theme } from "../theme.ts";

export interface ToolOutputProps {
	readonly calls: readonly ToolCall[];
}

export function ToolOutput({ calls }: ToolOutputProps): React.JSX.Element | null {
	if (calls.length === 0) return null;
	return (
		<Box flexDirection="column" marginLeft={2}>
			{calls.map((call) => (
				<Box key={call.callId} gap={1}>
					{call.done ? <Text color={theme.tool.done}>{"\u2713"}</Text> : <Spinner type="dots" />}
					<Text dimColor>{shortenToolName(call.name)}</Text>
					{call.done && typeof call.durationMs === "number" ? (
						<Text dimColor>({call.durationMs}ms)</Text>
					) : null}
				</Box>
			))}
		</Box>
	);
}

/** Strip the `mcp__memory__` prefix so the display is legible. */
function shortenToolName(name: string): string {
	const prefix = "mcp__memory__";
	return name.startsWith(prefix) ? name.slice(prefix.length) : name;
}
