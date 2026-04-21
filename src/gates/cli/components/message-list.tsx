/**
 * Scrollable conversation history.
 *
 * Renders the full list of displayed messages with user/assistant labels and
 * nested tool output. Ink's flex layout does the scrolling for us — when the
 * parent Box has `overflow="hidden"` and `flexGrow={1}`, the terminal
 * naturally clips the top as new content is appended.
 *
 * Each row is keyed by message id so React can reconcile efficiently when new
 * chunks arrive.
 */

import { Box, Text } from "ink";
import type React from "react";
import type { DisplayMessage } from "../state.ts";
import { ASSISTANT_PROMPT, theme, USER_PROMPT } from "../theme.ts";
import { ToolOutput } from "./tool-output.tsx";

export interface MessageListProps {
	readonly messages: readonly DisplayMessage[];
}

export function MessageList({ messages }: MessageListProps): React.JSX.Element {
	return (
		<Box flexDirection="column">
			{messages.map((msg) => (
				<MessageRow key={msg.id} message={msg} />
			))}
		</Box>
	);
}

interface MessageRowProps {
	readonly message: DisplayMessage;
}

function MessageRow({ message }: MessageRowProps): React.JSX.Element {
	switch (message.role) {
		case "user":
			return (
				<Box flexDirection="row" gap={1}>
					<Text color={theme.user.label}>{USER_PROMPT}</Text>
					<Text color={theme.user.text}>{message.text}</Text>
				</Box>
			);
		case "assistant":
			return (
				<Box flexDirection="column">
					<Box flexDirection="row" gap={1}>
						<Text color={theme.assistant.label}>{ASSISTANT_PROMPT}</Text>
						<Text color={message.interrupted ? theme.interrupted.text : theme.assistant.text}>
							{message.text}
						</Text>
					</Box>
					<ToolOutput calls={message.toolCalls} />
				</Box>
			);
		case "system":
			return <Text dimColor>{message.text}</Text>;
	}
}
