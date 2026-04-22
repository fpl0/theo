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
import { Header } from "./components/header.tsx";
import { InputArea } from "./components/input-area.tsx";
import { MessageList } from "./components/message-list.tsx";
import { StatusBar } from "./components/status-bar.tsx";
import { useEngine } from "./hooks.ts";
import {
	isOperatorCommand,
	type OperatorDeps,
	parseOperatorLine,
	runOperatorCommand,
} from "./operator.ts";

export interface AppProps {
	readonly engine: ChatEngine;
	readonly bus: EventBus;
	readonly onExit: () => void;
	/**
	 * Deps for Phase 13b/15 operator commands. When omitted, operator
	 * commands report "not configured" — useful for headless test wiring
	 * that only exercises the chat path.
	 */
	readonly operator?: OperatorDeps;
}

export function App({ engine, bus, onExit, operator }: AppProps): React.JSX.Element {
	const {
		state,
		messages,
		sessionId,
		inputHistory,
		stats,
		send,
		abort,
		resetSession,
		clearMessages,
		appendSystem,
	} = useEngine(engine, bus);

	const dispatchOperator = useCallback(
		(text: string): boolean => {
			const parsed = parseOperatorLine(text);
			if (parsed === null || !isOperatorCommand(parsed.name)) return false;
			if (operator === undefined) {
				appendSystem(`${parsed.name}: operator commands not configured`);
				return true;
			}
			void runOperatorCommand(operator, text)
				.then((result) => appendSystem(result.message))
				.catch((err: unknown) => {
					const message = err instanceof Error ? err.message : String(err);
					appendSystem(`${parsed.name}: ${message}`);
				});
			return true;
		},
		[appendSystem, operator],
	);

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
					case "/proposals":
					case "/approve":
					case "/reject":
					case "/redact":
					case "/consent":
					case "/cloud-audit":
					case "/degradation":
					case "/webhook-rotate":
						dispatchOperator(trimmed);
						return;
				}
				return;
			}
			if (trimmed.length === 0) return;
			send(text);
		},
		[
			send,
			onExit,
			resetSession,
			clearMessages,
			appendSystem,
			sessionId,
			state.phase,
			dispatchOperator,
		],
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
	const workspace = process.env["THEO_WORKSPACE"];
	const environment = process.env["THEO_ENV"];

	return (
		<Box flexDirection="column" height="100%">
			<Header
				{...(workspace !== undefined ? { workspace } : {})}
				{...(environment !== undefined ? { environment } : {})}
			/>
			<Box flexDirection="column" flexGrow={1} paddingX={1} paddingTop={1}>
				<MessageList messages={messages} />
			</Box>
			<StatusBar state={state} sessionId={sessionId} stats={stats} />
			<InputArea
				state={state}
				history={inputHistory}
				onSubmit={handleSubmit}
				onAbort={handleAbort}
			/>
		</Box>
	);
}
