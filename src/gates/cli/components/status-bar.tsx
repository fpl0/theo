/**
 * One-row status strip shown between the message list and the input area.
 *
 * Displays the coarse engine phase (idle / processing / streaming / error) and
 * the active session id (if any). Deliberately minimal — extra detail is
 * surfaced through the `/status` command.
 */

import { Box, Text } from "ink";
import type React from "react";
import type { TuiState } from "../state.ts";
import { theme } from "../theme.ts";

export interface StatusBarProps {
	readonly state: TuiState;
	readonly sessionId: string | null;
}

export function StatusBar({ state, sessionId }: StatusBarProps): React.JSX.Element {
	return (
		<Box flexDirection="row" paddingX={1} gap={2}>
			<Text color={theme.status.text} backgroundColor={theme.status.bg}>
				{" "}
				{describePhase(state)}{" "}
			</Text>
			{sessionId !== null ? <Text dimColor>session: {shortId(sessionId)}</Text> : null}
			{state.phase === "processing" ? <Text dimColor>thinking...</Text> : null}
			{state.phase === "streaming" ? <Text dimColor>streaming ({state.chunks} chunks)</Text> : null}
			{state.phase === "error" ? (
				<Text color={theme.error.text}>error: {state.message}</Text>
			) : null}
		</Box>
	);
}

function describePhase(state: TuiState): string {
	switch (state.phase) {
		case "idle":
			return "idle";
		case "processing":
			return "working";
		case "streaming":
			return "streaming";
		case "error":
			return "error";
	}
}

/** Show the last 6 chars of a ULID — enough to disambiguate, not enough to dominate. */
function shortId(id: string): string {
	return id.length <= 6 ? id : id.slice(-6);
}
