/**
 * Conversation history.
 *
 * Renders every `DisplayMessage` as a block: a colored prompt glyph, the
 * role-appropriate text, and any tool calls nested underneath (assistant
 * rows only). Turn boundaries are marked with a thin dim rule so the eye
 * can pick out where one exchange ends and the next begins.
 *
 * Ink clips the top of this list naturally when the terminal scrolls —
 * we do not attempt to own a scrollback viewport, since that fights with
 * the terminal emulator's own scroll handling.
 */

import { Box, Text } from "ink";
import type React from "react";
import type { DisplayMessage } from "../state.ts";
import { symbols, theme } from "../theme.ts";
import { RichText } from "./markdown.tsx";
import { ToolOutput } from "./tool-output.tsx";

export interface MessageListProps {
	readonly messages: readonly DisplayMessage[];
}

export function MessageList({ messages }: MessageListProps): React.JSX.Element {
	return (
		<Box flexDirection="column">
			{messages.map((msg, idx) => {
				const prev = idx > 0 ? messages[idx - 1] : undefined;
				const needsSeparator =
					prev !== undefined && prev.role === "assistant" && msg.role === "user";
				return (
					<Box key={msg.id} flexDirection="column">
						{needsSeparator ? <TurnSeparator /> : null}
						<MessageRow message={msg} />
					</Box>
				);
			})}
		</Box>
	);
}

/**
 * A thin faded rule between turns. Keeps visual density low while still
 * giving the reader somewhere to pause between user → assistant exchanges.
 */
function TurnSeparator(): React.JSX.Element {
	return (
		<Box marginY={0} paddingLeft={2}>
			<Text color={theme.separator}>· · ·</Text>
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
				<Box flexDirection="row">
					<Box marginRight={1}>
						<Text color={theme.user.label} bold>
							{symbols.userPrompt}
						</Text>
					</Box>
					<Box flexGrow={1}>
						<Text color={theme.user.text}>{message.text}</Text>
					</Box>
				</Box>
			);
		case "assistant": {
			const textColor = message.interrupted ? theme.interrupted : theme.assistant.text;
			return (
				<Box flexDirection="column">
					<Box flexDirection="row">
						<Box marginRight={1}>
							<Text color={theme.assistant.label} bold>
								{symbols.assistantPrompt}
							</Text>
						</Box>
						<Box flexGrow={1}>
							<RichText text={message.text} color={textColor} dimColor={message.interrupted} />
						</Box>
					</Box>
					<ToolOutput calls={message.toolCalls} />
				</Box>
			);
		}
		case "system":
			return (
				<Box paddingLeft={2}>
					<Text color={theme.system.text} italic>
						{message.text}
					</Text>
				</Box>
			);
	}
}
