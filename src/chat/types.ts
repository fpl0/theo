/**
 * Types for the chat engine.
 *
 * TurnResult is the value returned by ChatEngine.handleMessage(). It is a
 * discriminated union over `ok` — the caller decides what to do on error
 * without needing to read the response body.
 *
 * AgentConfig holds the tunables that flow into the SDK's query() call. All
 * fields are optional so the engine can apply sensible defaults.
 */

/** Result of a single chat turn. */
export type TurnResult =
	| { readonly ok: true; readonly response: string }
	| { readonly ok: false; readonly error: string };

/** Tunables for the agent runtime. Every field is optional — defaults below. */
export interface AgentConfig {
	readonly model?: string;
	readonly maxBudgetPerTurn?: number;
	readonly inactivityTimeoutMs?: number;
	/** Cosine similarity threshold for topic continuity. Default 0.7. */
	readonly topicContinuityThreshold?: number;
	/** Session depth (turn count) above which timeout is extended. Default 50. */
	readonly deepSessionThreshold?: number;
}

/** Coarse engine state — surfaced by gates for status displays. */
export type EngineState =
	| { readonly status: "running" }
	| { readonly status: "paused"; readonly queuedMessages: number }
	| { readonly status: "stopped"; readonly reason: string };
