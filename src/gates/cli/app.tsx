/**
 * Root Ink application for the CLI gate.
 *
 * Composes the message list, status bar, and input area. Handles slash commands
 * (before they reach the engine) and orchestrates Ctrl+C semantics:
 *   - idle: Ctrl+C exits via `onExit`
 *   - processing/streaming: Ctrl+C aborts the turn
 *
 * The component is deliberately thin — all orchestration lives in `useEngine`
 * so tests can exercise the logic without mounting React.
 */

import { Box } from "ink";
import type React from "react";
import { useCallback } from "react";
import type { ChatEngine } from "../../chat/engine.ts";
import type { EventBus } from "../../events/bus.ts";
import { resolveSlashCommand, SLASH_COMMANDS } from "./commands.ts";
import { InputArea } from "./components/input-area.tsx";
import { MessageList } from "./components/message-list.tsx";
import { StatusBar } from "./components/status-bar.tsx";
import { useEngine } from "./hooks.ts";

export interface AppProps {
	readonly engine: ChatEngine;
	readonly bus: EventBus;
	readonly onExit: () => void;
}

export function App({ engine, bus, onExit }: AppProps): React.JSX.Element {
	const {
		state,
		messages,
		sessionId,
		inputHistory,
		send,
		abort,
		resetSession,
		clearMessages,
		appendSystem,
	} = useEngine(engine, bus);

	const handleSubmit = useCallback(
		(text: string) => {
			const trimmed = text.trim();
			const command = resolveSlashCommand(trimmed);
			if (command !== null) {
				switch (command) {
					case "/quit":
						onExit();
						return;
					case "/reset":
						resetSession();
						appendSystem("session cleared");
						return;
					case "/clear":
						clearMessages();
						return;
					case "/status":
						appendSystem(
							`phase: ${state.phase}${sessionId !== null ? ` | session: ${sessionId}` : ""}`,
						);
						return;
					case "/memory":
						appendSystem("memory stats: (not yet wired)");
						return;
					case "/help": {
						const help = SLASH_COMMANDS.map(
							(cmd) =>
								`${cmd.name}${cmd.aliases.length > 0 ? ` (${cmd.aliases.join(", ")})` : ""} - ${cmd.description}`,
						).join("\n");
						appendSystem(help);
						return;
					}
				}
				return;
			}
			if (trimmed.length === 0) return;
			send(text);
		},
		[send, onExit, resetSession, clearMessages, appendSystem, sessionId, state.phase],
	);

	const handleAbort = useCallback(() => {
		if (state.phase === "processing" || state.phase === "streaming") {
			abort();
			return;
		}
		onExit();
	}, [abort, onExit, state.phase]);

	// Layout: message list grows to fill the terminal; status bar + input
	// sit pinned at the bottom. Native terminal scrollback handles history
	// scroll — Ink intentionally does not provide a scrollable viewport
	// primitive, and re-implementing one would collide with the terminal's
	// own scroll handling.
	return (
		<Box flexDirection="column" height="100%">
			<Box flexDirection="column" flexGrow={1}>
				<MessageList messages={messages} />
			</Box>
			<StatusBar state={state} sessionId={sessionId} />
			<InputArea
				state={state}
				history={inputHistory}
				onSubmit={handleSubmit}
				onAbort={handleAbort}
			/>
		</Box>
	);
}
