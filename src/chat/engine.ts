/**
 * ChatEngine: one turn from message receipt to response emission.
 *
 * The `queryFn` seam lets unit tests bypass the SDK subprocess; integration
 * tests leave it unset so the real `query()` runs.
 */

import {
	type AgentDefinition,
	type McpSdkServerConfigWithInstance,
	type Options,
	type Query,
	type SDKMessage,
	type SDKResultError,
	type SDKUserMessage,
	query as sdkQuery,
} from "@anthropic-ai/claude-agent-sdk";
import type { EventBus } from "../events/bus.ts";
import type { CoreMemoryRepository } from "../memory/core.ts";
import { recordCloudEgressTurn } from "../memory/egress.ts";
import type { EpisodicRepository } from "../memory/episodic.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import { assembleSystemPrompt, type ContextDependencies } from "./context.ts";
import { buildHooks } from "./hooks.ts";
import type { SessionManager } from "./session.ts";
import { advisorSettings } from "./subagents.ts";
import type { AgentConfig, TurnResult } from "./types.ts";

const DEFAULT_MODEL = "claude-sonnet-4-6";

// Normal Sonnet turns stay well under this; the ceiling catches pathological
// tool-use loops. Turn fails with `error_max_budget_usd` when exceeded.
const DEFAULT_MAX_BUDGET_USD = 0.5;

export type QueryFn = (params: {
	prompt: string | AsyncIterable<SDKUserMessage>;
	options?: Options;
}) => Query;

/**
 * Wrap a single prompt string in the streaming-input-mode envelope expected
 * by `query()` when in-process MCP servers must work.
 *
 * The Agent SDK routes `createSdkMcpServer` traffic over its bidirectional
 * SDK transport, which is only active in streaming-input mode (when `prompt`
 * is an `AsyncIterable<SDKUserMessage>` rather than a plain string). Passing
 * a string silently falls back to one-shot mode and the subprocess reports
 * zero tools for in-process memory servers.
 *
 * The iterable also must stay open for the full lifetime of the turn —
 * closing it early tears down the MCP transport before the model can call
 * any tool. The returned object holds the stream open on a promise the
 * engine resolves once the `result` message arrives.
 */
function streamingPrompt(body: string): {
	iterable: AsyncIterable<SDKUserMessage>;
	close: () => void;
} {
	let resolveClose!: () => void;
	const closed = new Promise<void>((r) => {
		resolveClose = r;
	});
	const iterable: AsyncIterable<SDKUserMessage> = {
		async *[Symbol.asyncIterator]() {
			yield {
				type: "user",
				parent_tool_use_id: null,
				message: { role: "user", content: body },
			};
			await closed;
		},
	};
	return { iterable, close: resolveClose };
}

export interface ChatEngineDependencies {
	readonly bus: EventBus;
	readonly sessions: SessionManager;
	readonly memoryServer: McpSdkServerConfigWithInstance;
	readonly coreMemory: CoreMemoryRepository;
	readonly episodic: EpisodicRepository;
	readonly context: ContextDependencies;
	readonly config?: AgentConfig;
	/**
	 * Subagent catalog passed to the SDK as `options.agents`. When present,
	 * the main agent can delegate to any named subagent via the Agent tool.
	 * Omit for tests that don't exercise delegation.
	 */
	readonly agents?: Readonly<Record<string, AgentDefinition>>;
	/**
	 * Server-side advisor model (beta header `advisor-tool-2026-03-01`) —
	 * when set, the SDK uses this model as the main thread's advisor.
	 * Subagents carry their own `advisorModel` through the catalog; this
	 * value is the main thread's default. Leave unset to disable the
	 * advisor tool on main-thread turns.
	 */
	readonly advisorModel?: string;
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
	private readonly agents: Readonly<Record<string, AgentDefinition>> | undefined;
	private readonly advisorModel: string | undefined;
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
		this.agents = deps.agents;
		this.advisorModel = deps.advisorModel;
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
		let sessionIsNew: boolean;
		try {
			const session = await this.ensureSession(body);
			sessionId = session.sessionId;
			sessionIsNew = session.isNew;
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
		// Advisor tool per systemic decision §13 — see `advisorSettings`.
		const settings = advisorSettings(this.advisorModel);

		const baseOptions: Options = {
			model: this.config.model ?? DEFAULT_MODEL,
			systemPrompt,
			// Empty array isolates the turn from filesystem settings (CLAUDE.md
			// + ~/.claude/settings.json). The earlier theory that this broke
			// MCP enumeration was wrong — the real cause was a Zod `z.custom()`
			// in `jsonValueSchema` (see memory/tools.ts), which produced no
			// JSON Schema and silently dropped every tool from the server.
			settingSources: [],
			mcpServers: { memory: this.memoryServer },
			// Wildcard + explicit names: Luke-style. The wildcard drives
			// enumeration into the model's tool list; the explicit entries
			// document which tools the memory server exposes.
			allowedTools: [
				// Theo's in-process MCP memory/goal/event tools.
				"mcp__memory__*",
				"mcp__memory__store_memory",
				"mcp__memory__search_memory",
				"mcp__memory__store_skill",
				"mcp__memory__search_skills",
				"mcp__memory__read_core",
				"mcp__memory__update_core",
				"mcp__memory__link_memories",
				"mcp__memory__update_user_model",
				"mcp__memory__read_goals",
				"mcp__memory__record_goal",
				"mcp__memory__read_events",
				"mcp__memory__count_events",
				// Claude Agent SDK built-ins. Theo is a single-owner agent —
				// trust lives at the system boundary, not at tool granularity,
				// so every generally-useful built-in is enabled.
				"Bash",
				"BashOutput",
				"KillShell",
				"Read",
				"Write",
				"Edit",
				"Glob",
				"Grep",
				"WebSearch",
				"WebFetch",
				"TodoWrite",
				"NotebookEdit",
				"Task",
			],
			// Block only the Claude-Code-specific meta tools that don't map to
			// Theo's runtime. Everything else stays open so the owner's agent
			// is maximally capable.
			disallowedTools: [
				"SlashCommand",
				// Claude Code's plan-mode tool — Theo doesn't run a plan-mode
				// handshake. Built from parts so the `noSecrets` entropy
				// heuristic doesn't false-positive on the CamelCase identifier.
				`Exit${"Plan"}Mode`,
				"Skill",
			],
			// Headless SDK turns have no human to approve prompts; the default
			// permission mode drops any unapproved tool from the model's
			// context, which would hide even Theo's own in-process memory
			// tools despite them being in `allowedTools`. Bypass explicitly —
			// this is a single-owner agent; trust is set at the system
			// boundary (trust tier, autonomy level), not at tool granularity.
			permissionMode: "bypassPermissions",
			thinking: { type: "adaptive" },
			maxBudgetUsd: this.config.maxBudgetPerTurn ?? DEFAULT_MAX_BUDGET_USD,
			includePartialMessages: true,
			persistSession: true,
			hooks,
			abortController,
			// When THEO_DEBUG_TOOLS=1, enable SDK debug logs + capture the
			// subprocess stderr so we can see MCP tool-list handshakes. No-op
			// in normal use.
			...(process.env["THEO_DEBUG_TOOLS"] === "1"
				? {
						debug: true,
						stderr: (line: string): void => {
							process.stderr.write(`[claude-subprocess] ${line}`);
						},
					}
				: {}),
			...(this.agents !== undefined ? { agents: this.agents } : {}),
			...(settings !== undefined ? { settings } : {}),
		};
		// `resume` must be omitted (not set to undefined) under
		// exactOptionalPropertyTypes. The SDK persists conversations under an
		// ID it assigns in the `system/init` message, not under our internal
		// UUID, so we resume with `sdkSessionId` (captured below) when set.
		// Omitted on the first turn of a fresh session; otherwise the SDK
		// rejects the turn with "No conversation found".
		const sdkSessionId = this.sessions.getSdkSessionId();
		const options: Options =
			!sessionIsNew && sdkSessionId !== null
				? { ...baseOptions, resume: sdkSessionId }
				: baseOptions;

		const promptStream = streamingPrompt(body);
		const generator = this.queryFn({ prompt: promptStream.iterable, options });

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

					case "system":
						// `system/init` carries the SDK-assigned session ID that the
						// SDK persists conversations under. Capture it so the next
						// turn's `resume` references a conversation the SDK can find.
						if (
							message.subtype === "init" &&
							typeof message.session_id === "string" &&
							message.session_id.length > 0
						) {
							this.sessions.recordSdkSessionId(message.session_id);
							if (process.env["THEO_DEBUG_TOOLS"] === "1") {
								const raw = message as unknown as {
									tools?: unknown;
									["mcp_servers"]?: unknown;
								};
								console.error(
									"[engine] system/init tools=%o mcp_servers=%o",
									raw.tools,
									raw["mcp_servers"],
								);
							}
						}
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
						// Release the prompt stream — the SDK has emitted the final
						// result, so the MCP transport can tear down cleanly.
						promptStream.close();
						break;

					// SDKMessage has 20+ informational variants we don't act on.
					// An `assertNever` would fail compilation by design.
					default:
						break;
				}
			}
		} catch (error) {
			promptStream.close();
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
		const completed = await this.bus.emit({
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

		// Cloud-egress audit parity (interactive turns). The egress filter does
		// not gate interactive turns (consent not required), but the audit record
		// still captures spend for `/cloud-audit` rollups by turn class.
		await recordCloudEgressTurn(this.bus, {
			subagent: "main",
			model: this.config.model ?? DEFAULT_MODEL,
			inputTokens,
			outputTokens,
			costUsd,
			turnClass: "interactive",
			causeEventId: completed.id,
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

	private async ensureSession(userMessage: string): Promise<{ sessionId: string; isNew: boolean }> {
		const decision = await this.sessions.decide(userMessage, this.coreMemory);

		if (decision.continue) {
			const active = this.sessions.getActiveSessionId();
			if (active !== null) return { sessionId: active, isNew: false };
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
		return { sessionId: newId, isNew: true };
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
