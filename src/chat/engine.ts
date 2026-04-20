/**
 * ChatEngine: the agent runtime.
 *
 * Orchestrates one full turn from message receipt to response emission:
 *   1. `message.received` audit event
 *   2. Session manager decision + prompt assembly
 *   3. `turn.started` event
 *   4. SDK `query()` — streaming chunks via ephemeral events, final result
 *      captured for token accounting
 *   5. `turn.completed` (success) or `turn.failed` (error) event
 *
 * The engine is testable via a `queryFn` seam — the real SDK `query()` runs
 * a subprocess, which is impractical in unit tests. Integration tests would
 * construct a ChatEngine without overriding the seam and let the real SDK
 * call through.
 */

import {
	type McpSdkServerConfigWithInstance,
	type Options,
	type Query,
	type SDKResultError,
	query as sdkQuery,
} from "@anthropic-ai/claude-agent-sdk";
import type { EventBus } from "../events/bus.ts";
import type { CoreMemoryRepository } from "../memory/core.ts";
import type { EpisodicRepository } from "../memory/episodic.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import { assembleSystemPrompt, type ContextDependencies } from "./context.ts";
import { buildHooks } from "./hooks.ts";
import type { SessionManager } from "./session.ts";
import type { AgentConfig, TurnResult } from "./types.ts";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

/** Default model — Sonnet 4.6. Production can override via AgentConfig.model. */
const DEFAULT_MODEL = "claude-sonnet-4-6";

/**
 * Default per-turn budget: $0.50. A complex tool-use loop on Sonnet rarely
 * exceeds this; a bug in the loop (e.g. pathological retry) would. Turn fails
 * with `error_max_budget_usd` when exceeded.
 */
const DEFAULT_MAX_BUDGET_USD = 0.5;

/** Shape of the SDK's query() function; used for the test seam. */
export type QueryFn = (params: { prompt: string; options?: Options }) => Query;

// ---------------------------------------------------------------------------
// ChatEngine
// ---------------------------------------------------------------------------

/** Dependencies wired into the ChatEngine constructor. */
export interface ChatEngineDependencies {
	readonly bus: EventBus;
	readonly sessions: SessionManager;
	readonly memoryServer: McpSdkServerConfigWithInstance;
	readonly coreMemory: CoreMemoryRepository;
	readonly episodic: EpisodicRepository;
	readonly context: ContextDependencies;
	readonly config?: AgentConfig;
	/**
	 * Seam for tests. When undefined, the real SDK `query()` is used.
	 * Tests inject a mock that yields canned `SDKMessage` variants.
	 */
	readonly queryFn?: QueryFn;
}

export class ChatEngine {
	private readonly bus: EventBus;
	private readonly sessions: SessionManager;
	private readonly memoryServer: McpSdkServerConfigWithInstance;
	private readonly coreMemory: CoreMemoryRepository;
	private readonly episodic: EpisodicRepository;
	private readonly context: ContextDependencies;
	private readonly config: AgentConfig;
	private readonly queryFn: QueryFn;

	constructor(deps: ChatEngineDependencies) {
		this.bus = deps.bus;
		this.sessions = deps.sessions;
		this.memoryServer = deps.memoryServer;
		this.coreMemory = deps.coreMemory;
		this.episodic = deps.episodic;
		this.context = deps.context;
		this.config = deps.config ?? {};
		this.queryFn = deps.queryFn ?? sdkQuery;
	}

	/**
	 * Process one incoming message end-to-end. Always returns a TurnResult;
	 * errors surface as `{ ok: false, error }`, never as thrown exceptions.
	 */
	async handleMessage(body: string, gate: string): Promise<TurnResult> {
		// 1. Audit: the message arrived. Emitted before any failure can occur so
		//    the event log always knows a message was received.
		await this.bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body, channel: gate },
			metadata: { gate },
		});

		// 2. Session decision
		let sessionId: string;
		try {
			sessionId = await this.ensureSession(body);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			return { ok: false, error: message };
		}

		// 3. Assemble system prompt. Errors here (e.g. empty memory guard) are
		//    turn failures — emit turn.failed and return.
		let systemPrompt: string;
		const startTime = Date.now();
		try {
			systemPrompt = await assembleSystemPrompt(this.context, body);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			const durationMs = Date.now() - startTime;
			await this.bus.emit({
				type: "turn.failed",
				version: 1,
				actor: "system",
				data: {
					sessionId,
					errorType: "error_during_execution",
					errors: [message],
					durationMs,
				},
				metadata: { sessionId, gate },
			});
			return { ok: false, error: message };
		}

		// 4. Emit turn.started now that we have a session and a prompt.
		await this.bus.emit({
			type: "turn.started",
			version: 1,
			actor: "theo",
			data: { sessionId, prompt: body },
			metadata: { sessionId, gate },
		});

		// 5. Fire the SDK query. The async generator is consumed fully to
		//    guarantee subprocess cleanup; mid-stream exits call generator.return().
		const hooks = buildHooks({
			bus: this.bus,
			episodic: this.episodic,
			sessionId,
			// Phase 10 seam: interactive turns come from the owner. The
			// causation-chain walker (Phase 13b) will replace this with
			// per-turn effective trust.
			trustTier: "owner" satisfies TrustTier,
		});
		const baseOptions: Options = {
			model: this.config.model ?? DEFAULT_MODEL,
			systemPrompt,
			// Empty array: critical isolation. No external CLAUDE.md, no user
			// settings — the assembled system prompt is the sole source of truth.
			settingSources: [],
			mcpServers: { memory: this.memoryServer },
			// Auto-approve memory tools; the agent doesn't need permission to
			// use its own memory. Cross-cutting allowlist scoping by effective
			// trust lands in Phase 13b.
			allowedTools: ["mcp__memory__*"],
			thinking: { type: "adaptive" },
			maxBudgetUsd: this.config.maxBudgetPerTurn ?? DEFAULT_MAX_BUDGET_USD,
			includePartialMessages: true,
			persistSession: true,
			hooks,
		};
		// `resume` must be omitted (not set to undefined) under
		// exactOptionalPropertyTypes. It is only set when we are rejoining an
		// existing SDK session.
		const options: Options =
			this.sessions.getActiveSessionId() === sessionId
				? { ...baseOptions, resume: sessionId }
				: baseOptions;

		const generator = this.queryFn({ prompt: body, options });

		let responseBody = "";
		let inputTokens = 0;
		let outputTokens = 0;
		let costUsd = 0;
		let failure: SDKResultError | null = null;

		try {
			for await (const message of generator) {
				switch (message.type) {
					case "stream_event":
						this.handleStreamEvent(message.event, sessionId);
						break;

					case "assistant":
						// Full assistant message — extract text from content
						// blocks. This may be overwritten if the SDK later emits
						// a successful result (the `result` field is authoritative).
						responseBody = extractAssistantText(message.message.content);
						break;

					case "result":
						if (message.subtype === "success") {
							responseBody = message.result;
							inputTokens = message.usage.input_tokens;
							outputTokens = message.usage.output_tokens;
							costUsd = message.total_cost_usd;
						} else {
							failure = message;
						}
						break;

					// All other SDK message variants (system, user_replay,
					// compact_boundary, status, api_retry, local_command_output,
					// hook_*, tool_progress, auth_status, task_*,
					// session_state_changed, files_persisted,
					// tool_use_summary, rate_limit, elicitation_complete,
					// prompt_suggestion) are informational — no action needed.
					default:
						break;
				}
			}
		} catch (error) {
			// The generator itself threw (network, subprocess crash, etc.).
			// Ensure the subprocess is cleaned up, then emit turn.failed.
			try {
				await generator.return();
			} catch {
				// Cleanup is best-effort.
			}
			const message = error instanceof Error ? error.message : String(error);
			const durationMs = Date.now() - startTime;
			await this.bus.emit({
				type: "turn.failed",
				version: 1,
				actor: "system",
				data: {
					sessionId,
					errorType: "error_during_execution",
					errors: [message],
					durationMs,
				},
				metadata: { sessionId, gate },
			});
			return { ok: false, error: message };
		}

		const durationMs = Date.now() - startTime;

		// 6. Terminal outcome
		if (failure !== null) {
			await this.bus.emit({
				type: "turn.failed",
				version: 1,
				actor: "system",
				data: {
					sessionId,
					errorType: failure.subtype,
					errors: failure.errors,
					durationMs,
				},
				metadata: { sessionId, gate },
			});
			return { ok: false, error: failure.subtype };
		}

		const totalTokens = inputTokens + outputTokens;
		await this.bus.emit({
			type: "turn.completed",
			version: 1,
			actor: "theo",
			data: {
				sessionId,
				responseBody,
				durationMs,
				inputTokens,
				outputTokens,
				totalTokens,
				costUsd,
			},
			metadata: { sessionId, gate },
		});

		// Record the turn in the session manager — updates activity clock,
		// increments depth, caches the message embedding for next time's
		// topic-continuity check.
		await this.sessions.recordTurn(body);

		return { ok: true, response: responseBody };
	}

	/**
	 * Imperatively release the current session (user said `/reset`, etc.).
	 * Emits `session.released` so the audit trail reflects it.
	 */
	async resetSession(reason = "user_request"): Promise<void> {
		const released = this.sessions.releaseSession();
		if (released !== null) {
			await this.bus.emit({
				type: "session.released",
				version: 1,
				actor: "system",
				data: { sessionId: released, reason },
				metadata: { sessionId: released },
			});
		}
	}

	// -------------------------------------------------------------------------
	// Internal helpers
	// -------------------------------------------------------------------------

	/**
	 * Run the session manager's decision and start/release sessions as needed.
	 * Emits `session.released` when a session is rotated out.
	 */
	private async ensureSession(userMessage: string): Promise<string> {
		const decision = await this.sessions.decide(userMessage, this.coreMemory);

		if (decision.continue) {
			const active = this.sessions.getActiveSessionId();
			if (active !== null) return active;
			// Decision said continue but there's no active session — fall
			// through to start one. Safety net; the session manager should
			// not return { continue: true } when activeSessionId is null.
		}

		// Not continuing — release any active session first.
		const released = this.sessions.releaseSession();
		if (released !== null) {
			await this.bus.emit({
				type: "session.released",
				version: 1,
				actor: "system",
				data: { sessionId: released, reason: decision.continue ? "active" : decision.reason },
				metadata: { sessionId: released },
			});
		}

		const newId = await this.sessions.startSession(this.coreMemory);
		await this.bus.emit({
			type: "session.created",
			version: 1,
			actor: "system",
			data: { sessionId: newId },
			metadata: { sessionId: newId },
		});
		return newId;
	}

	/**
	 * Emit an ephemeral `stream.chunk` for text_delta events. All other
	 * BetaRawMessageStreamEvent variants (content_block_start, message_delta,
	 * thinking_delta, etc.) are ignored — they're not user-visible text.
	 */
	private handleStreamEvent(
		event: import("@anthropic-ai/sdk/resources/beta/messages/messages.mjs").BetaRawMessageStreamEvent,
		sessionId: string,
	): void {
		if (event.type !== "content_block_delta") return;
		if (event.delta.type !== "text_delta") return;
		this.bus.emitEphemeral({
			type: "stream.chunk",
			data: { text: event.delta.text, sessionId },
		});
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract human-readable text from a BetaMessage's content array. Skips
 * non-text blocks (tool_use, thinking, etc.).
 */
function extractAssistantText(
	content: import("@anthropic-ai/sdk/resources/beta/messages/messages.mjs").BetaContentBlock[],
): string {
	const parts: string[] = [];
	for (const block of content) {
		if (block.type === "text") {
			parts.push(block.text);
		}
	}
	return parts.join("");
}

// Note on exhaustiveness: SDKMessage has 20+ variants (system, user_replay,
// compact_boundary, status, api_retry, local_command_output, hook_*,
// tool_progress, auth_status, task_*, session_state_changed, files_persisted,
// tool_use_summary, rate_limit, elicitation_complete, prompt_suggestion). The
// switch above intentionally handles only the variants that affect turn
// semantics (`stream_event`, `assistant`, `result`) and ignores the rest via
// `default: break`. An `assertNever` guard would fail compilation since we
// don't handle every variant — and that's by design.
