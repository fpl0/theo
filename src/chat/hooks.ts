/**
 * SDK hook implementations.
 *
 * Hooks bridge the Claude Agent SDK's lifecycle into Theo's event system and
 * episodic memory. Each hook is wrapped in `safeHook()`: a failure inside a
 * hook emits a `hook.failed` event and returns an empty result so the agent
 * loop continues.
 *
 * Hooks live at the boundary between two worlds:
 *   - SDK: strongly typed via HookInput / HookJSONOutput
 *   - Theo: event log, episodic memory, core memory, privacy filter
 *
 * PreCompact and Stop hooks archive transcript messages as episodes so the
 * conversation survives SDK-initiated compaction. The message.received event
 * (emitted by the chat engine directly) is the audit trail; episodes are the
 * memory that feeds future RRF searches.
 */

import type {
	HookCallbackMatcher,
	HookInput,
	HookJSONOutput,
	PostCompactHookInput,
	PreCompactHookInput,
	PreToolUseHookInput,
	StopHookInput,
	UserPromptSubmitHookInput,
} from "@anthropic-ai/claude-agent-sdk";
import type { EventBus } from "../events/bus.ts";
import type { EpisodicRepository } from "../memory/episodic.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import { checkPrivacy } from "../memory/privacy.ts";

// ---------------------------------------------------------------------------
// Transcript parsing (for PreCompact)
// ---------------------------------------------------------------------------

/** A single message extracted from the SDK's transcript file. */
export interface TranscriptMessage {
	readonly role: "user" | "assistant";
	readonly text: string;
}

/**
 * Parse the SDK's transcript file contents into structured messages.
 *
 * The SDK's transcript format is a JSON-lines log. Each line is one record;
 * records without a user/assistant role or without a `content` field are
 * skipped. Unparseable lines (metadata, separators, partials) are skipped
 * rather than fatally failing the PreCompact hook.
 *
 * Pure function — tested directly.
 */
export function parseTranscript(raw: string): readonly TranscriptMessage[] {
	const messages: TranscriptMessage[] = [];
	for (const line of raw.split("\n")) {
		const trimmed = line.trim();
		if (trimmed.length === 0) continue;
		let parsed: unknown;
		try {
			parsed = JSON.parse(trimmed);
		} catch {
			continue;
		}
		if (parsed === null || typeof parsed !== "object") continue;
		const record = parsed as Record<string, unknown>;
		const role = record["role"];
		const content = record["content"];
		if ((role === "user" || role === "assistant") && typeof content === "string") {
			messages.push({ role, text: content });
		}
	}
	return messages;
}

// ---------------------------------------------------------------------------
// Safe hook wrapper
// ---------------------------------------------------------------------------

/** A hook callback as typed by the SDK (HookCallback). */
type HookFn = (
	input: HookInput,
	toolUseId: string | undefined,
	options: { signal: AbortSignal },
) => Promise<HookJSONOutput>;

/**
 * Wrap a hook so exceptions are logged via `hook.failed` rather than
 * propagating into the SDK. The SDK treats thrown hooks as a fatal turn
 * error; Theo prefers to continue the turn and record the failure.
 */
export function safeHook(fn: HookFn, bus: EventBus): HookFn {
	return async (input, toolUseId, opts) => {
		try {
			return await fn(input, toolUseId, opts);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			await bus.emit({
				type: "hook.failed",
				version: 1,
				actor: "system",
				data: { hookEvent: input.hook_event_name, error: message },
				metadata: {},
			});
			return {};
		}
	};
}

// ---------------------------------------------------------------------------
// Hook builders
// ---------------------------------------------------------------------------

/** Dependencies required to build the hook suite. */
export interface HookDependencies {
	readonly bus: EventBus;
	readonly episodic: EpisodicRepository;
	readonly sessionId: string;
	/**
	 * Effective trust tier for this turn. Phase 10 seams this in; the full
	 * per-turn causation walker lands in Phase 13b. For now, the chat engine
	 * passes the interactive-channel owner tier.
	 */
	readonly trustTier: TrustTier;
}

/** SDK-shaped map of hooks ready to be passed to `query()`. */
export type HookMap = Partial<Record<string, HookCallbackMatcher[]>>;

/**
 * SDK hook-event names. These are external-API identifiers mandated by the
 * Agent SDK (`HookEvent` union) and must stay PascalCase. Kept as string
 * constants rather than inline PascalCase object keys so the naming
 * convention lint rule stays strict on first-party code.
 */
const HOOK_USER_PROMPT_SUBMIT = "UserPromptSubmit";
const HOOK_PRE_TOOL_USE = "PreToolUse";
const HOOK_PRE_COMPACT = "PreCompact";
const HOOK_POST_COMPACT = "PostCompact";
const HOOK_STOP = "Stop";

/** The PreToolUse matcher that scopes the privacy gate to store_memory. */
const STORE_MEMORY_TOOL = "mcp__memory__store_memory";

/**
 * Build the hook suite for a turn.
 *
 * Each callback is wrapped in `safeHook()` before being returned, so the
 * caller does not need to worry about exception handling.
 */
export function buildHooks(deps: HookDependencies): HookMap {
	return {
		[HOOK_USER_PROMPT_SUBMIT]: [{ hooks: [safeHook(makeUserPromptSubmit(deps), deps.bus)] }],
		[HOOK_PRE_TOOL_USE]: [
			{
				matcher: STORE_MEMORY_TOOL,
				hooks: [safeHook(makePreToolUse(deps), deps.bus)],
			},
		],
		[HOOK_PRE_COMPACT]: [{ hooks: [safeHook(makePreCompact(deps), deps.bus)] }],
		[HOOK_POST_COMPACT]: [{ hooks: [safeHook(makePostCompact(deps), deps.bus)] }],
		[HOOK_STOP]: [{ hooks: [safeHook(makeStop(deps), deps.bus)] }],
	};
}

// ---------------------------------------------------------------------------
// Individual hook factories
// ---------------------------------------------------------------------------

/**
 * UserPromptSubmit: persist the user's message as an episode.
 *
 * The `message.received` event (emitted by the chat engine) is the audit
 * trail. The episode created here is the memory — it has an embedding,
 * participates in RRF, and links to knowledge nodes.
 */
function makeUserPromptSubmit(deps: HookDependencies): HookFn {
	return async (input): Promise<HookJSONOutput> => {
		const { prompt } = input as UserPromptSubmitHookInput;
		await deps.episodic.append({
			sessionId: deps.sessionId,
			role: "user",
			body: prompt,
			actor: "user",
		});
		return {};
	};
}

/**
 * PreToolUse on `store_memory`: enforce the privacy filter.
 *
 * If the content being stored exceeds the trust tier's limit (e.g. a
 * restricted-tier pattern passed from an external-tier source), deny the
 * tool call with a reason string the model can understand.
 */
function makePreToolUse(deps: HookDependencies): HookFn {
	return async (input): Promise<HookJSONOutput> => {
		const { tool_input } = input as PreToolUseHookInput;
		const typed = tool_input as { body?: unknown };
		if (typeof typed.body !== "string") {
			return {};
		}
		const decision = checkPrivacy(typed.body, deps.trustTier);
		if (!decision.allowed) {
			return {
				hookSpecificOutput: {
					hookEventName: "PreToolUse",
					permissionDecision: "deny",
					permissionDecisionReason: decision.reason,
				},
			};
		}
		return {};
	};
}

/**
 * PreCompact: archive assistant messages as episodes before the SDK
 * summarizes the transcript.
 *
 * The SDK provides `transcript_path` (from BaseHookInput) — a file on disk.
 * Reading it here is best-effort: if the file is missing, unreadable, or
 * empty, the hook returns cleanly without emitting episodes. A missing
 * transcript is not a failure mode worth crashing the turn over.
 */
function makePreCompact(deps: HookDependencies): HookFn {
	return async (input): Promise<HookJSONOutput> => {
		const compact = input as PreCompactHookInput;
		await deps.bus.emit({
			type: "session.compacting",
			version: 1,
			actor: "system",
			data: { sessionId: deps.sessionId, trigger: compact.trigger },
			metadata: { sessionId: deps.sessionId },
		});

		const transcriptPath = compact.transcript_path;
		if (typeof transcriptPath !== "string" || transcriptPath.length === 0) {
			return {};
		}

		let raw: string;
		try {
			raw = await Bun.file(transcriptPath).text();
		} catch {
			// File missing or unreadable — nothing to archive.
			return {};
		}

		const messages = parseTranscript(raw);
		for (const msg of messages) {
			if (msg.role !== "assistant") continue;
			if (msg.text.length === 0) continue;
			await deps.episodic.append({
				sessionId: deps.sessionId,
				role: "assistant",
				body: msg.text,
				actor: "theo",
			});
		}

		return {};
	};
}

/**
 * PostCompact: record the SDK's compaction summary as an event.
 *
 * The summary is Theo's only record of what the transcript contained before
 * compaction collapsed it. Persisting it in the event log means the session
 * stays replayable.
 */
function makePostCompact(deps: HookDependencies): HookFn {
	return async (input): Promise<HookJSONOutput> => {
		const { compact_summary } = input as PostCompactHookInput;
		await deps.bus.emit({
			type: "session.compacted",
			version: 1,
			actor: "system",
			data: { sessionId: deps.sessionId, summary: compact_summary },
			metadata: { sessionId: deps.sessionId },
		});
		return {};
	};
}

/**
 * Stop: record the assistant's final message as an episode.
 *
 * The SDK fills `last_assistant_message` when the turn ends cleanly. If it's
 * missing (e.g. tool-only turn, interrupted), skip the episode silently.
 */
function makeStop(deps: HookDependencies): HookFn {
	return async (input): Promise<HookJSONOutput> => {
		const stop = input as StopHookInput;
		const body = stop.last_assistant_message;
		if (typeof body !== "string" || body.length === 0) {
			return {};
		}
		await deps.episodic.append({
			sessionId: deps.sessionId,
			role: "assistant",
			body,
			actor: "theo",
		});
		return {};
	};
}
