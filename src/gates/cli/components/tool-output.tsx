/**
 * Tool-call chips rendered below an assistant message.
 *
 * Each chip is one line:
 *
 *     ◦  spinner  search_memory
 *     ✓            store_memory   12ms
 *
 * The visual weight stays below the assistant prose: muted name, subtle
 * icon, light-gray duration. Done / pending / error states each get a
 * dedicated icon and color so a quick glance tells you what happened
 * without reading the name.
 */

import { Spinner } from "@inkjs/ui";
import { Box, Text } from "ink";
import type React from "react";
import type { ToolCall } from "../state.ts";
import { symbols, theme } from "../theme.ts";

export interface ToolOutputProps {
	readonly calls: readonly ToolCall[];
}

export function ToolOutput({ calls }: ToolOutputProps): React.JSX.Element | null {
	if (calls.length === 0) return null;
	return (
		<Box flexDirection="column" marginLeft={2} marginTop={0}>
			{calls.map((call) => (
				<ToolChip key={call.callId} call={call} />
			))}
		</Box>
	);
}

function ToolChip({ call }: { readonly call: ToolCall }): React.JSX.Element {
	return (
		<Box gap={1}>
			<ToolIcon done={call.done} />
			<Text color={theme.tool.name}>{shortenToolName(call.name)}</Text>
			{call.done && typeof call.durationMs === "number" ? (
				<Text color={theme.chip.separator}>{formatDuration(call.durationMs)}</Text>
			) : null}
		</Box>
	);
}

function ToolIcon({ done }: { readonly done: boolean }): React.JSX.Element {
	if (done) {
		return <Text color={theme.tool.done}>{symbols.toolDone}</Text>;
	}
	// `@inkjs/ui` Spinner renders a `<Box>` internally, so it cannot be
	// nested inside a `<Text>`. Return it as a Box-adjacent node.
	return <Spinner type="dots" />;
}

/**
 * `mcp__memory__store_memory` → `store_memory`. Keeps the chip readable
 * without losing information (the prefix is the same on every tool).
 */
function shortenToolName(name: string): string {
	const prefix = "mcp__memory__";
	return name.startsWith(prefix) ? name.slice(prefix.length) : name;
}

function formatDuration(ms: number): string {
	if (ms < 1_000) return `${ms}ms`;
	return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}
