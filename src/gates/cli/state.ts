/**
 * CLI gate state types.
 *
 * `TuiState` is the coarse lifecycle of a turn. Components watch it to decide
 * whether to show a spinner, whether Ctrl+C aborts or quits, and whether the
 * input area accepts new text.
 *
 * `DisplayMessage` is what the message list renders — derived from durable
 * events plus in-flight streaming state.
 *
 * Both types are discriminated unions so exhaustive switches catch new
 * variants at compile time.
 */

export type TuiState =
	| { readonly phase: "idle" }
	| { readonly phase: "processing"; readonly startedAt: number }
	| { readonly phase: "streaming"; readonly chunks: number }
	| { readonly phase: "error"; readonly message: string };

/** A single tool call observed during a turn. */
export interface ToolCall {
	readonly callId: string;
	readonly name: string;
	readonly done: boolean;
	readonly durationMs?: number;
}

/** One rendered line in the conversation history. */
export interface DisplayMessage {
	readonly id: string;
	readonly role: "user" | "assistant" | "system";
	readonly text: string;
	readonly timestamp: Date;
	readonly toolCalls: readonly ToolCall[];
	/** True while stream.chunk events are still arriving for this message. */
	readonly streaming: boolean;
	/** True if the user aborted mid-stream. */
	readonly interrupted: boolean;
}
