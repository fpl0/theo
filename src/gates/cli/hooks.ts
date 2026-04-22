/**
 * React hooks that bridge the Ink app to the chat engine and event bus.
 *
 * These are the only TUI-to-engine contact points. Components never import
 * the engine or bus directly — they consume state via these hooks. That keeps
 * rendering concerns separate from orchestration concerns.
 *
 * Note on floating promises: `engine.handleMessage()` returns a Promise but
 * event handlers in useInput cannot be async (they return void). We explicitly
 * `void` the promise after wiring its completion into React state.
 */

import { useCallback, useEffect, useState } from "react";
import type { ChatEngine } from "../../chat/engine.ts";
import type { EventBus } from "../../events/bus.ts";
import type { DisplayMessage, ToolCall, TuiState } from "./state.ts";
import { MAX_INPUT_HISTORY } from "./theme.ts";

// ---------------------------------------------------------------------------
// useInputHistory: shell-style Up/Down cycle
// ---------------------------------------------------------------------------

export interface UseInputHistoryResult {
	readonly history: readonly string[];
	readonly push: (text: string) => void;
}

export function useInputHistory(maxSize: number = MAX_INPUT_HISTORY): UseInputHistoryResult {
	const [history, setHistory] = useState<readonly string[]>([]);

	const push = useCallback(
		(text: string) => {
			if (text.length === 0) return;
			setHistory((prev) => {
				// De-duplicate consecutive repeats (shell convention).
				if (prev[0] === text) return prev;
				const next = [text, ...prev];
				return next.length > maxSize ? next.slice(0, maxSize) : next;
			});
		},
		[maxSize],
	);

	return { history, push };
}

// ---------------------------------------------------------------------------
// useStream: subscribe to the ephemeral stream for a specific session
// ---------------------------------------------------------------------------

export interface UseStreamResult {
	readonly text: string;
	readonly done: boolean;
	readonly reset: () => void;
}

/**
 * Subscribe to `stream.chunk` / `stream.done` ephemerals. The hook filters by
 * session id when one is provided, so concurrent background turns don't leak
 * into the foreground TUI.
 */
export function useStream(bus: EventBus, sessionId: string | null): UseStreamResult {
	const [chunks, setChunks] = useState<readonly string[]>([]);
	const [done, setDone] = useState(false);

	useEffect(() => {
		const offChunk = bus.onEphemeral("stream.chunk", (event) => {
			if (sessionId !== null && event.data.sessionId !== sessionId) return;
			setChunks((prev) => [...prev, event.data.text]);
			setDone(false);
		});
		const offDone = bus.onEphemeral("stream.done", (event) => {
			if (sessionId !== null && event.data.sessionId !== sessionId) return;
			setDone(true);
		});
		return () => {
			offChunk();
			offDone();
		};
	}, [bus, sessionId]);

	const reset = useCallback(() => {
		setChunks([]);
		setDone(false);
	}, []);

	const text = chunks.join("");
	return { text, done, reset };
}

// ---------------------------------------------------------------------------
// useToolCalls: ordered list of active + completed tool calls
// ---------------------------------------------------------------------------

export interface UseToolCallsResult {
	readonly calls: readonly ToolCall[];
	readonly reset: () => void;
}

export function useToolCalls(bus: EventBus, sessionId: string | null): UseToolCallsResult {
	const [calls, setCalls] = useState<readonly ToolCall[]>([]);

	useEffect(() => {
		const offStart = bus.onEphemeral("tool.start", (event) => {
			if (sessionId !== null && event.data.sessionId !== sessionId) return;
			const started: ToolCall = {
				callId: event.data.callId,
				name: event.data.name,
				done: false,
			};
			setCalls((prev) => [...prev, started]);
		});
		const offDone = bus.onEphemeral("tool.done", (event) => {
			if (sessionId !== null && event.data.sessionId !== sessionId) return;
			setCalls((prev) =>
				prev.map((c) =>
					c.callId === event.data.callId
						? { ...c, done: true, durationMs: event.data.durationMs }
						: c,
				),
			);
		});
		return () => {
			offStart();
			offDone();
		};
	}, [bus, sessionId]);

	const reset = useCallback(() => {
		setCalls([]);
	}, []);

	return { calls, reset };
}

// ---------------------------------------------------------------------------
// useEngine: the main orchestration hook
// ---------------------------------------------------------------------------

export interface SessionStats {
	readonly costUsd: number;
	readonly inputTokens: number;
	readonly outputTokens: number;
}

export interface UseEngineResult {
	readonly state: TuiState;
	readonly messages: readonly DisplayMessage[];
	readonly sessionId: string | null;
	readonly inputHistory: readonly string[];
	readonly stats: SessionStats;
	readonly send: (text: string) => void;
	readonly abort: () => void;
	readonly resetSession: () => void;
	readonly clearMessages: () => void;
	readonly appendSystem: (text: string) => void;
}

export function useEngine(engine: ChatEngine, bus: EventBus): UseEngineResult {
	const [state, setState] = useState<TuiState>({ phase: "idle" });
	const [messages, setMessages] = useState<readonly DisplayMessage[]>([]);
	const [sessionId, setSessionId] = useState<string | null>(null);
	const [stats, setStats] = useState<SessionStats>({
		costUsd: 0,
		inputTokens: 0,
		outputTokens: 0,
	});
	const history = useInputHistory();
	// Pass `sessionId` (state) — not a ref — so the inner hooks re-subscribe
	// when the session rotates. Ephemeral events for stale sessions are
	// filtered out inside the hooks.
	const stream = useStream(bus, sessionId);
	const tools = useToolCalls(bus, sessionId);

	// Track session lifecycle from durable events so the status bar stays honest.
	useEffect(() => {
		bus.on("session.created", async (event) => {
			setSessionId(event.data.sessionId);
			// New session → zero the running totals so the status bar always
			// reflects what THIS session has spent.
			setStats({ costUsd: 0, inputTokens: 0, outputTokens: 0 });
		});
		bus.on("session.released", async () => {
			setSessionId(null);
		});
		// Accumulate cost and tokens from every completed turn — the event
		// log is authoritative (projected from SDK result messages).
		bus.on("turn.completed", async (event) => {
			setStats((prev) => ({
				costUsd: prev.costUsd + (event.data.costUsd ?? 0),
				inputTokens: prev.inputTokens + (event.data.inputTokens ?? 0),
				outputTokens: prev.outputTokens + (event.data.outputTokens ?? 0),
			}));
		});
	}, [bus]);

	// As chunks arrive, update the in-flight assistant message in place.
	useEffect(() => {
		if (state.phase !== "streaming") return;
		setMessages((prev) => {
			const last = prev[prev.length - 1];
			if (last === undefined || last.role !== "assistant" || !last.streaming) return prev;
			if (last.text === stream.text) return prev;
			const next = [...prev];
			next[next.length - 1] = { ...last, text: stream.text };
			return next;
		});
	}, [stream.text, state.phase]);

	// As tool.start/tool.done events arrive, attach them to the in-flight
	// assistant message.
	useEffect(() => {
		if (state.phase !== "streaming" && state.phase !== "processing") return;
		setMessages((prev) => {
			const last = prev[prev.length - 1];
			if (last === undefined || last.role !== "assistant" || !last.streaming) return prev;
			if (sameToolCalls(last.toolCalls, tools.calls)) return prev;
			const next = [...prev];
			next[next.length - 1] = { ...last, toolCalls: tools.calls };
			return next;
		});
	}, [tools.calls, state.phase]);

	// If the first chunk arrives while we're still in `processing`, transition
	// to `streaming` automatically — the user doesn't care about the boundary.
	useEffect(() => {
		if (stream.text.length === 0) return;
		setState((prev) => {
			if (prev.phase === "processing") {
				return { phase: "streaming", chunks: 1 };
			}
			if (prev.phase === "streaming") {
				return { phase: "streaming", chunks: prev.chunks + 1 };
			}
			return prev;
		});
	}, [stream.text]);

	const send = useCallback(
		(text: string) => {
			if (state.phase === "processing" || state.phase === "streaming") return;
			const now = new Date();
			const stamp = `${String(now.getTime())}-${String(Math.random()).slice(2, 8)}`;
			const userMsg: DisplayMessage = {
				id: `user-${stamp}`,
				role: "user",
				text,
				timestamp: now,
				toolCalls: [],
				streaming: false,
				interrupted: false,
			};
			const assistantMsg: DisplayMessage = {
				id: `assistant-${stamp}`,
				role: "assistant",
				text: "",
				timestamp: now,
				toolCalls: [],
				streaming: true,
				interrupted: false,
			};
			setMessages((prev) => [...prev, userMsg, assistantMsg]);
			history.push(text);
			stream.reset();
			tools.reset();
			setState({ phase: "processing", startedAt: now.getTime() });

			void engine
				.handleMessage(text, "cli")
				.then((result) => {
					setMessages((prev) => {
						const last = prev[prev.length - 1];
						if (last === undefined || last.role !== "assistant" || !last.streaming) return prev;
						const next = [...prev];
						// Use the already-streamed text if any chunks arrived — the
						// setMessages effect in useStream keeps `last.text` current.
						// Otherwise fall back to the authoritative TurnResult text.
						const finalText =
							last.text.length > 0
								? last.text
								: result.ok
									? result.response
									: `error: ${result.error}`;
						next[next.length - 1] = {
							...last,
							text: finalText,
							streaming: false,
							interrupted: false,
						};
						return next;
					});
					if (result.ok) {
						setState({ phase: "idle" });
					} else {
						setState({ phase: "error", message: result.error });
					}
				})
				.catch((err: unknown) => {
					const message = err instanceof Error ? err.message : String(err);
					setMessages((prev) => {
						const last = prev[prev.length - 1];
						if (last === undefined || last.role !== "assistant" || !last.streaming) return prev;
						const next = [...prev];
						next[next.length - 1] = {
							...last,
							text: `error: ${message}`,
							streaming: false,
						};
						return next;
					});
					setState({ phase: "error", message });
				});
		},
		[engine, history, state.phase, stream, tools],
	);

	const abort = useCallback(() => {
		engine.abortCurrentTurn();
		setMessages((prev) => {
			const last = prev[prev.length - 1];
			if (last === undefined || last.role !== "assistant" || !last.streaming) return prev;
			const next = [...prev];
			next[next.length - 1] = {
				...last,
				text: `${last.text}\n[interrupted]`,
				streaming: false,
				interrupted: true,
			};
			return next;
		});
		setState({ phase: "idle" });
	}, [engine]);

	const resetSession = useCallback(() => {
		// Fire-and-forget; the session.released event updates sessionId via the
		// bus subscription above.
		void engine.resetSession("user_request").catch(() => {});
		setMessages([]);
	}, [engine]);

	const clearMessages = useCallback(() => {
		setMessages([]);
	}, []);

	const appendSystem = useCallback((text: string) => {
		setMessages((prev) => [
			...prev,
			{
				id: `sys-${String(Date.now())}-${String(prev.length)}`,
				role: "system",
				text,
				timestamp: new Date(),
				toolCalls: [],
				streaming: false,
				interrupted: false,
			},
		]);
	}, []);

	return {
		state,
		messages,
		sessionId,
		inputHistory: history.history,
		stats,
		send,
		abort,
		resetSession,
		clearMessages,
		appendSystem,
	};
}

function sameToolCalls(a: readonly ToolCall[], b: readonly ToolCall[]): boolean {
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i++) {
		const x = a[i];
		const y = b[i];
		if (x === undefined || y === undefined) return false;
		if (x.callId !== y.callId || x.done !== y.done || x.durationMs !== y.durationMs) return false;
	}
	return true;
}
