/**
 * ChatEngine: one turn from message receipt to response emission.
 *
 * The `queryFn` seam lets unit tests bypass the SDK subprocess; integration
 * tests leave it unset so the real `query()` runs.
 */

import {
	type McpSdkServerConfigWithInstance,
	type Options,
	type Query,
	type SDKMessage,
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

const DEFAULT_MODEL = "claude-sonnet-4-6";

// Normal Sonnet turns stay well under this; the ceiling catches pathological
// tool-use loops. Turn fails with `error_max_budget_usd` when exceeded.
const DEFAULT_MAX_BUDGET_USD = 0.5;

export type QueryFn = (params: { prompt: string; options?: Options }) => Query;

export interface ChatEngineDependencies {
	readonly bus: EventBus;
	readonly sessions: SessionManager;
	readonly memoryServer: McpSdkServerConfigWithInstance;
	readonly coreMemory: CoreMemoryRepository;
	readonly episodic: EpisodicRepository;
	readonly context: ContextDependencies;
	readonly config?: AgentConfig;
	/** Test seam. When undefined, the real SDK `query()` is used. */
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
	/**
	 * Abort controller for the in-flight turn, or null when idle. Gates call
	 * `abortCurrentTurn()` to cancel; the SDK's `Options.abortController`
	 * forwards the signal through the subprocess bridge.
	 */
	private currentAbort: AbortController | null = null;

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
	 * Abort the in-flight turn. No-op when the engine is idle. The turn loop
	 * catches the resulting abort error and emits `turn.failed`.
	 */
	abortCurrentTurn(): void {
		this.currentAbort?.abort();
	}

	/** Always returns a TurnResult — errors are values, never thrown. */
	async handleMessage(body: string, gate: string): Promise<TurnResult> {
		// Emit message.received before any failure can occur so the event log
		// always records the arrival even if the turn blows up.
		await this.bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body, channel: gate },
			metadata: { gate },
		});

		let sessionId: string;
		try {
			sessionId = await this.ensureSession(body);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			return { ok: false, error: message };
		}

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

		await this.bus.emit({
			type: "turn.started",
			version: 1,
			actor: "theo",
			data: { sessionId, prompt: body },
			metadata: { sessionId, gate },
		});

		const hooks = buildHooks({
			bus: this.bus,
			episodic: this.episodic,
			sessionId,
			trustTier: "owner" satisfies TrustTier,
		});
		const abortController = new AbortController();
		this.currentAbort = abortController;
		const baseOptions: Options = {
			model: this.config.model ?? DEFAULT_MODEL,
			systemPrompt,
			// Empty array isolates the turn from any external CLAUDE.md or user
			// settings — the assembled system prompt is the sole source of truth.
			settingSources: [],
			mcpServers: { memory: this.memoryServer },
			allowedTools: ["mcp__memory__*"],
			thinking: { type: "adaptive" },
			maxBudgetUsd: this.config.maxBudgetPerTurn ?? DEFAULT_MAX_BUDGET_USD,
			includePartialMessages: true,
			persistSession: true,
			hooks,
			abortController,
		};
		// `resume` must be omitted (not set to undefined) under
		// exactOptionalPropertyTypes.
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
		// callId -> wall-clock ms when tool.start was emitted. Used to compute
		// durationMs when the matching tool_result arrives in a later user
		// message. `Date.now()` reads are cheap; no need to thread a clock.
		const toolStartTimes = new Map<string, number>();

		try {
			for await (const message of generator) {
				switch (message.type) {
					case "stream_event":
						this.handleStreamEvent(message.event, sessionId);
						break;

					case "assistant":
						// May be overwritten by a later `result` message, whose
						// `result` field is authoritative.
						responseBody = extractAssistantText(message.message.content);
						this.emitToolStarts(message, sessionId, toolStartTimes);
						break;

					case "user":
						this.emitToolDones(message, sessionId, toolStartTimes);
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

					// SDKMessage has 20+ informational variants we don't act on.
					// An `assertNever` would fail compilation by design.
					default:
						break;
				}
			}
		} catch (error) {
			try {
				await generator.return();
			} catch {
				// Cleanup is best-effort.
			}
			const message = error instanceof Error ? error.message : String(error);
			const durationMs = Date.now() - startTime;
			this.bus.emitEphemeral({ type: "stream.done", data: { sessionId } });
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
			this.currentAbort = null;
			return { ok: false, error: message };
		}

		this.bus.emitEphemeral({ type: "stream.done", data: { sessionId } });

		const durationMs = Date.now() - startTime;

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
			this.currentAbort = null;
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

		this.currentAbort = null;
		return { ok: true, response: responseBody };
	}

	/** Release the current session and emit `session.released`. */
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

	private async ensureSession(userMessage: string): Promise<string> {
		const decision = await this.sessions.decide(userMessage, this.coreMemory);

		if (decision.continue) {
			const active = this.sessions.getActiveSessionId();
			if (active !== null) return active;
			// decision.continue with no active id shouldn't happen, but fall
			// through to start one rather than throw.
		}

		const released = this.sessions.releaseSession();
		if (released !== null && !decision.continue) {
			await this.bus.emit({
				type: "session.released",
				version: 1,
				actor: "system",
				data: { sessionId: released, reason: decision.reason },
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

	/**
	 * Scan an assistant message for `tool_use` content blocks and emit a
	 * `tool.start` ephemeral for each. The call ID is cached with the current
	 * wall clock so `tool.done` can compute duration when the matching
	 * `tool_result` arrives in a subsequent user message.
	 */
	private emitToolStarts(
		message: Extract<SDKMessage, { type: "assistant" }>,
		sessionId: string,
		toolStartTimes: Map<string, number>,
	): void {
		for (const block of message.message.content) {
			if (block.type !== "tool_use") continue;
			toolStartTimes.set(block.id, Date.now());
			// Tool input is modelled as `unknown`. Stringify defensively — the
			// ephemeral carries a short label for the TUI to display, not a
			// round-trippable value.
			const input = typeof block.input === "string" ? block.input : JSON.stringify(block.input);
			this.bus.emitEphemeral({
				type: "tool.start",
				data: { name: block.name, input, callId: block.id, sessionId },
			});
		}
	}

	/**
	 * Scan a user (turn-continuation) message for `tool_result` content blocks
	 * and emit `tool.done` for each, computing duration against the cached
	 * start time. Orphan results (no matching start) are skipped silently —
	 * they should never happen during a turn Theo initiated, but skipping is
	 * safer than asserting.
	 */
	private emitToolDones(
		message: Extract<SDKMessage, { type: "user" }>,
		sessionId: string,
		toolStartTimes: Map<string, number>,
	): void {
		const content = message.message.content;
		if (!Array.isArray(content)) return;
		const now = Date.now();
		for (const block of content) {
			if (block.type !== "tool_result") continue;
			const startedAt = toolStartTimes.get(block.tool_use_id);
			if (startedAt === undefined) continue;
			toolStartTimes.delete(block.tool_use_id);
			this.bus.emitEphemeral({
				type: "tool.done",
				data: { callId: block.tool_use_id, durationMs: now - startedAt, sessionId },
			});
		}
	}
}

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
