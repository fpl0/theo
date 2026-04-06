# Phase 10: Agent Runtime

## Motivation

This is where Theo becomes an agent. Everything built so far — events, memory, retrieval, tools —
converges into the agent runtime. The SDK's `query()` function runs the agent loop: thinking, tool
calls, memory operations, reasoning. The hooks bridge the SDK's lifecycle into Theo's event system.
The context assembly builds a system prompt from memory tiers so the agent starts every session with
relevant, personalized context.

This is the highest-stakes phase. Get it right, and Theo processes messages end-to-end. Get it
wrong, and nothing works.

## Depends on

- **Phase 3** — Event bus (hooks emit events)
- **Phase 9** — MCP memory tools (agent needs memory access)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/chat/context.ts` | System prompt assembly from memory tiers |
| `src/chat/session.ts` | Session manager — lifecycle, timeout, invalidation |
| `src/chat/engine.ts` | `ChatEngine` — main `handleMessage()` method, SDK `query()` integration, streaming |
| `src/chat/hooks.ts` | Hook implementations for all SDK lifecycle points |
| `src/chat/types.ts` | Chat-related types (turn result, session state, hook types) |
| `tests/chat/context.test.ts` | System prompt assembly tests |
| `tests/chat/session.test.ts` | Session lifecycle tests |
| `tests/chat/engine.test.ts` | Engine integration tests (mocked SDK) |
| `tests/chat/hooks.test.ts` | Hook event emission tests |

## Design Decisions

### Context Assembly (`context.ts`)

The system prompt is assembled fresh for every new session from Theo's memory tiers:

```typescript
interface ContextDependencies {
  readonly coreMemory: CoreMemoryRepository;
  readonly userModel: UserModelRepository;
  readonly retrieval: RetrievalService;
  readonly skills: SkillRepository;       // NEW — for active skill retrieval
  readonly embeddings: EmbeddingService;   // NEW — for topic continuity
}

async function assembleSystemPrompt(
  deps: ContextDependencies,
  userMessage: string,
): Promise<string> {
  // 1. Persona — who Theo is (never truncated)
  const persona = await deps.coreMemory.readSlot("persona");

  // 2. Goals — what Theo is working on (never truncated)
  const goals = await deps.coreMemory.readSlot("goals");

  // 3. User Model — who the owner is (budget-capped)
  const userModel = await deps.userModel.getDimensions();

  // 4. Current Context — recent activity, active tasks (budget-capped)
  const context = await deps.coreMemory.readSlot("context");

  // 5. Relevant Memories — RRF search for the incoming message (budget-capped)
  const memories = await deps.retrieval.search(userMessage, { limit: 15 });

  // 6. Active Skills — procedural knowledge matching the incoming message
  const skills = await deps.skills.findByTrigger(userMessage, 5);

  const prompt = buildPrompt({ persona, goals, userModel, context, memories, skills });

  // Guard: a system prompt shorter than 50 chars means memory is empty.
  // The agent would have no identity, no instructions — refuse to proceed.
  if (prompt.length < 50) {
    throw new Error("System prompt too short — memory may be empty. Run onboarding first.");
  }

  return prompt;
}
```

The `buildPrompt()` function formats each section with clear headers. Token budget management:
persona and goals are never truncated. User model, context, and memories are capped to fit within a
budget (configurable, default ~4000 tokens estimated by character count / 4).

### Session Manager (`session.ts`)

Sessions are short-lived working memory:

```typescript
class SessionManager {
  private activeSessionId: string | null = null;
  private lastActivityAt: Date | null = null;
  private coreMemoryHash: string | null = null;
  private readonly inactivityTimeoutMs: number;

  constructor(config: { inactivityTimeoutMs?: number }) {
    this.inactivityTimeoutMs = config.inactivityTimeoutMs ?? 15 * 60 * 1000; // 15 min
  }

  async shouldStartNewSession(coreMemory: CoreMemoryRepository): Promise<boolean> {
    // New session if: no active session, timed out, or core memory changed
    if (!this.activeSessionId) return true;
    if (this.isTimedOut()) return true;

    const currentHash = await coreMemory.hash();
    if (currentHash !== this.coreMemoryHash) return true;

    return false;
  }

  startSession(): string {
    this.activeSessionId = ulid();
    this.lastActivityAt = new Date();
    return this.activeSessionId;
  }

  getActiveSessionId(): string | null {
    return this.activeSessionId;
  }

  recordActivity(): void {
    this.lastActivityAt = new Date();
  }

  releaseSession(reason: string): string | null {
    const released = this.activeSessionId;
    this.activeSessionId = null;
    this.lastActivityAt = null;
    this.coreMemoryHash = null;
    return released;
  }

  private isTimedOut(): boolean {
    if (!this.lastActivityAt) return true;
    return Date.now() - this.lastActivityAt.getTime() > this.inactivityTimeoutMs;
  }
}
```

Session release triggers: inactivity timeout, core memory change, explicit user request.

### Smart Session Management

The session manager uses multiple signals beyond simple timeout to decide whether to continue or
start a new session:

**Topic continuity**: Compare the embedding of the incoming message against the last message in the
current session. If cosine similarity exceeds a threshold (default 0.7), the topic is continuous
even after timeout.

**Session depth tracking**: Track how many turns deep the current session is. Deep sessions (>50
turns) accumulate noise — starting fresh with curated context from memory produces better results.

**Self-model calibration**: Session decisions are recorded as predictions in the
`session_management` self-model domain. User corrections ("we were still talking about X" or "start
fresh") record outcomes. The heuristic adapts over time.

**Cost implications**: Continuing a session = maximum cache hit (cheapest). Starting fresh = full
context assembly (most expensive, but best quality when context is stale). The system optimizes for
response quality first, cost second.

### Chat Engine (`engine.ts`)

The main orchestrator. Handles the full lifecycle: event emission, session management, system prompt
assembly, SDK `query()` invocation, streaming, result extraction, and token accounting.

```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";
import type {
  SDKAssistantMessage,
  SDKPartialAssistantMessage,
  SDKResultMessage,
} from "@anthropic-ai/claude-agent-sdk";

class ChatEngine {
  constructor(
    private readonly bus: EventBus,
    private readonly sessions: SessionManager,
    private readonly memoryServer: McpServer,
    private readonly coreMemory: CoreMemoryRepository,
    private readonly deps: ContextDependencies,
    private readonly config: AgentConfig,
  ) {}

  async handleMessage(body: string, gate: string): Promise<TurnResult> {
    // 1. Emit message.received — audit trail in the event log
    await this.bus.emit({
      type: "message.received",
      version: 1,
      actor: "user",
      data: { body, channel: gate },
      metadata: { gate },
    });

    // 2. Determine session
    const needsNew = await this.sessions.shouldStartNewSession(this.coreMemory);
    if (needsNew) {
      const oldSession = this.sessions.releaseSession("new_message_after_timeout_or_change");
      if (oldSession) {
        await this.bus.emit({
          type: "session.released",
          version: 1,
          actor: "system",
          data: { sessionId: oldSession, reason: "new_message_after_timeout_or_change" },
          metadata: { sessionId: oldSession },
        });
      }
    }
    const sessionId = needsNew
      ? this.sessions.startSession()
      : this.sessions.getActiveSessionId()!;

    // 3. Assemble system prompt
    const systemPrompt = await assembleSystemPrompt(this.deps, body);

    // 4. Run SDK query
    const startTime = Date.now();
    await this.bus.emit({
      type: "turn.started",
      version: 1,
      actor: "theo",
      data: { sessionId, prompt: body },
      metadata: { sessionId, gate },
    });

    const generator = query({
      prompt: body,
      options: {
        model: this.config.model ?? "claude-sonnet-4-6",
        systemPrompt,
        settingSources: [],
        mcpServers: { memory: this.memoryServer },
        allowedTools: ["mcp__memory__*"],
        thinking: { type: "adaptive" },
        maxBudgetUsd: this.config.maxBudgetPerTurn ?? 0.50,
        resume: needsNew ? undefined : sessionId,
        persistSession: true,
        includePartialMessages: true,
        hooks: buildHooks(sessionId, this.bus, this.deps.episodic),
      },
    });

    // 5. Consume the async generator — extract streaming chunks and final result
    let responseBody = "";
    let inputTokens = 0;
    let outputTokens = 0;
    let costUsd = 0;

    for await (const message of generator) {
      switch (message.type) {
        case "stream_event":
          this.handleStreamEvent(message, sessionId);
          break;

        case "assistant":
          // Full assistant message — extract text from content blocks
          responseBody = extractTextFromContentBlocks(message.message.content);
          break;

        case "result":
          if (message.subtype === "success") {
            responseBody = message.result;
            inputTokens = message.usage.input_tokens;
            outputTokens = message.usage.output_tokens;
            costUsd = message.total_cost_usd;
          } else {
            // error_during_execution, input_required, etc.
            const durationMs = Date.now() - startTime;
            await this.bus.emit({
              type: "turn.failed",
              version: 1,
              actor: "system",
              data: {
                sessionId,
                errorType: message.subtype,
                errors: message.errors,
                durationMs,
              },
              metadata: { sessionId },
            });
            return { ok: false, error: message.subtype };
          }
          break;

        case "system":
          // init, status — log for debugging but no action needed
          break;

        default: {
          // Exhaustive check — if the SDK adds new message types, TypeScript catches it
          const _exhaustive: never = message;
          void _exhaustive;
        }
      }
    }

    // 6. Record completion with token accounting
    const durationMs = Date.now() - startTime;
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
        totalTokens: inputTokens + outputTokens,
        costUsd,
      },
      metadata: { sessionId },
    });

    this.sessions.recordActivity();
    return { ok: true, response: responseBody };
  }

  // Emit streaming chunks as ephemeral events for gates to display
  private handleStreamEvent(message: SDKPartialAssistantMessage, sessionId: string): void {
    const event = message.event;

    // BetaRawMessageStreamEvent has multiple types. We care about content_block_delta
    // which carries incremental text.
    if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
      this.bus.emitEphemeral({
        type: "stream.chunk",
        data: { text: event.delta.text, sessionId },
      });
    }
  }

  resetSession(): void {
    this.sessions.releaseSession("user_request");
  }
}
```

#### Streaming Flow

The full streaming path from SDK to user:

```text
SDK generates token
  → query() yields SDKPartialAssistantMessage (type: "stream_event")
  → engine.handleStreamEvent() checks for content_block_delta with text_delta
  → bus.emitEphemeral({ type: "stream.chunk", data: { text, sessionId } })
  → gate (CLI/Telegram) receives ephemeral event, writes to output
```

Streaming is enabled by setting `includePartialMessages: true` in the query options. The async
generator then yields `SDKPartialAssistantMessage` messages with `type: "stream_event"`. Each has an
`event` field containing a `BetaRawMessageStreamEvent` from the Anthropic API. The relevant event
type is `content_block_delta` with a `text_delta` delta, which carries the incremental text.

Streaming chunks are ephemeral events — they skip the event log (no write amplification) and go
directly through the bus to subscribed gates. The final response is captured from the
`SDKResultSuccess.result` field and persisted in the `turn.completed` event.

#### Token Accounting

The SDK provides complete usage data on the result message:

```typescript
// From SDKResultSuccess:
const inputTokens = result.usage.input_tokens;
const outputTokens = result.usage.output_tokens;
const costUsd = result.total_cost_usd;
const durationMs = result.duration_ms;

// Per-model breakdown (when subagents use different models):
// result.modelUsage is a Record<string, { input_tokens, output_tokens }>
```

These are persisted in the `turn.completed` event for cost tracking and budgeting over time.

#### Budget Controls

The `maxBudgetUsd` option caps spending per turn. If the agent exceeds the budget, the SDK
terminates the turn and returns an error result. This prevents runaway costs from complex tool-use
loops.

```typescript
maxBudgetUsd: this.config.maxBudgetPerTurn ?? 0.50,
```

The per-turn budget is configurable. The `turn.completed` event records actual cost, enabling
aggregate budget tracking (daily/monthly caps) as a future projection on the event log.

### Hooks (`hooks.ts`)

Hooks bridge the SDK lifecycle into Theo's event system. The SDK expects hooks as
`Partial<Record<HookEvent, HookCallbackMatcher[]>>` where each `HookCallbackMatcher` has an optional
`matcher` (for filtering by tool name), a `hooks` array of async callbacks, and an optional
`timeout`.

```typescript
import type {
  HookCallbackMatcher,
  HookInput,
  HookJSONOutput,
  UserPromptSubmitHookInput,
  PreToolUseHookInput,
  PreCompactHookInput,
  PostCompactHookInput,
  StopHookInput,
} from "@anthropic-ai/claude-agent-sdk";

function buildHooks(
  sessionId: string,
  bus: EventBus,
  episodic: EpisodicRepository,
): Partial<Record<string, HookCallbackMatcher[]>> {
  return {
    UserPromptSubmit: [
      {
        hooks: [
          async (
            input: HookInput,
            _toolUseID: string | undefined,
            { signal }: { signal: AbortSignal },
          ): Promise<HookJSONOutput> => {
            const { prompt } = input as UserPromptSubmitHookInput;
            // Persist the user's message as an episode — this is memory, not just audit.
            // The message.received event (emitted by handleMessage) is the audit trail.
            // The episode is the memory — it feeds future RRF searches and context assembly.
            await episodic.append({
              sessionId,
              role: "user",
              body: prompt,
              actor: "user",
            });
            return {};
          },
        ],
      },
    ],

    PreToolUse: [
      {
        matcher: "mcp__memory__store_memory",
        hooks: [
          async (
            input: HookInput,
            _toolUseID: string | undefined,
            { signal }: { signal: AbortSignal },
          ): Promise<HookJSONOutput> => {
            const { tool_input } = input as PreToolUseHookInput;
            const typedInput = tool_input as { body?: string; sensitivity?: string };

            if (typedInput.body) {
              const decision = checkPrivacy(typedInput.body, "owner");
              if (!decision.allowed) {
                return {
                  hookSpecificOutput: {
                    hookEventName: "PreToolUse",
                    permissionDecision: "deny",
                    permissionDecisionReason: decision.reason,
                  },
                };
              }
            }
            // Allow — return empty (no hookSpecificOutput means proceed)
            return {};
          },
        ],
      },
    ],

    PreCompact: [
      {
        hooks: [
          async (
            input: HookInput,
            _toolUseID: string | undefined,
            { signal }: { signal: AbortSignal },
          ): Promise<HookJSONOutput> => {
            await bus.emit({
              type: "session.compacting",
              version: 1,
              actor: "system",
              data: { sessionId, trigger: (input as PreCompactHookInput).trigger },
              metadata: { sessionId },
            });

            // Archive transcript as episodes before the SDK summarizes it.
            //
            // PreCompactHookInput does NOT have a messages field. The transcript
            // is available at input.transcript_path (from BaseHookInput) — a file
            // path to the session's conversation history.
            const transcriptPath = (input as PreCompactHookInput).transcript_path;
            if (transcriptPath) {
              const transcriptText = await Bun.file(transcriptPath).text();
              const messages = parseTranscript(transcriptText);

              for (const msg of messages) {
                if (msg.role === "assistant") {
                  await episodic.append({
                    sessionId,
                    role: "assistant",
                    body: msg.text,
                    actor: "theo",
                  });
                }
              }
            }

            return {};
          },
        ],
      },
    ],

    PostCompact: [
      {
        hooks: [
          async (
            input: HookInput,
            _toolUseID: string | undefined,
            { signal }: { signal: AbortSignal },
          ): Promise<HookJSONOutput> => {
            const { compact_summary } = input as PostCompactHookInput;
            await bus.emit({
              type: "session.compacted",
              version: 1,
              actor: "system",
              data: { sessionId, summary: compact_summary },
              metadata: { sessionId },
            });
            return {};
          },
        ],
      },
    ],

    Stop: [
      {
        hooks: [
          async (
            input: HookInput,
            _toolUseID: string | undefined,
            { signal }: { signal: AbortSignal },
          ): Promise<HookJSONOutput> => {
            const stopInput = input as StopHookInput;

            // Record the assistant's final message as an episode
            if (stopInput.last_assistant_message) {
              await episodic.append({
                sessionId,
                role: "assistant",
                body: stopInput.last_assistant_message,
                actor: "theo",
              });
            }

            return {};
          },
        ],
      },
    ],
  };
}
```

#### Transcript Parsing for PreCompact

The `PreCompactHookInput` provides `transcript_path` (from `BaseHookInput`), not a messages array.
The transcript file contains the session's conversation history in the SDK's internal format. A
`parseTranscript()` utility reads and extracts structured messages from it:

```typescript
interface TranscriptMessage {
  readonly role: "user" | "assistant";
  readonly text: string;
}

function parseTranscript(raw: string): readonly TranscriptMessage[] {
  // Parse the SDK's transcript format.
  // The exact format depends on the SDK version — start with JSON lines,
  // adapt if the SDK uses a different serialization.
  const lines = raw.split("\n").filter((line) => line.trim().length > 0);
  const messages: TranscriptMessage[] = [];

  for (const line of lines) {
    try {
      const parsed = JSON.parse(line) as { role?: string; content?: string };
      if (parsed.role === "user" || parsed.role === "assistant") {
        messages.push({ role: parsed.role, text: parsed.content ?? "" });
      }
    } catch {
      // Skip lines that aren't valid JSON — may be metadata or separators
    }
  }

  return messages;
}
```

#### Hook Safety

Every hook callback is wrapped in try/catch at the call site to prevent a hook failure from crashing
the agent loop. If a hook throws, the error is logged and the turn continues. The SDK provides
`AbortSignal` for cooperative cancellation — long-running hooks should check `signal.aborted`.

```typescript
// Wrapper applied to every hook callback before passing to the SDK
function safeHook(
  fn: (input: HookInput, toolUseID: string | undefined, opts: { signal: AbortSignal }) => Promise<HookJSONOutput>,
  bus: EventBus,
): typeof fn {
  return async (input, toolUseID, opts) => {
    try {
      return await fn(input, toolUseID, opts);
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
```

### Message vs Episode: Dual Recording Explained

The same user message is recorded in two places that serve different purposes:

| Record | Where | Purpose | Written by |
| -------- | ------- | --------- | ------------ |
| `message.received` event | Event log | Audit trail — when did this message arrive, from which gate | `handleMessage()` |
| Episode row | Episodic memory table | Memory — feeds RRF search, context assembly, future recall | `UserPromptSubmit` hook |

The event log is the immutable history of everything that happened. The episode is a projection
optimized for retrieval — it has an embedding, links to knowledge nodes, and participates in the
memory lifecycle (consolidation, summarization). Both are needed. Removing the event would lose the
audit trail. Removing the episode would break memory search.

### Types (`types.ts`)

```typescript
interface TurnResult {
  readonly ok: boolean;
  readonly response?: string;
  readonly error?: string;
}

interface AgentConfig {
  readonly model?: string;
  readonly maxBudgetPerTurn?: number;
  readonly inactivityTimeoutMs?: number;
}

// Discriminated union for engine state
type EngineState =
  | { readonly status: "running" }
  | { readonly status: "paused"; readonly queuedMessages: number }
  | { readonly status: "stopped"; readonly reason: string };
```

### SDK Configuration Summary

| Option | Value | Rationale |
| -------- | ------- | ----------- |
| `systemPrompt` | Assembled from memory tiers | Theo's identity comes from its memory, not static config |
| `settingSources: []` | Empty array | Critical isolation. No external CLAUDE.md, no user settings. The system prompt is the sole source of instructions |
| `allowedTools: ["mcp__memory__*"]` | Auto-approve memory tools | The agent doesn't need permission to use its own memory |
| `thinking: { type: "adaptive" }` | Adaptive extended thinking | Complex tasks get deeper reasoning automatically |
| `maxBudgetUsd` | Configurable, default $0.50 | Prevents runaway costs from complex tool-use loops |
| `includePartialMessages: true` | Enable streaming | Gates receive tokens in real-time via ephemeral events |
| `persistSession: true` | Session durability | Sessions survive process restarts |
| `resume` | Session ID or undefined | Resume active session, or start fresh on timeout/change |

## Definition of Done

- [ ] `assembleSystemPrompt()` builds prompt from all memory tiers
- [ ] System prompt includes persona, goals, user model, context, and RRF search results
- [ ] System prompt guard rejects prompts shorter than 50 characters
- [ ] `SessionManager` creates new sessions on first message
- [ ] `SessionManager` reuses session within inactivity window
- [ ] `SessionManager` releases session on timeout or core memory change
- [ ] `ChatEngine.handleMessage()` processes a message end-to-end: event, context, SDK, response
- [ ] `handleMessage()` streams chunks via ephemeral `stream.chunk` events
- [ ] `handleMessage()` extracts token counts and cost from `SDKResultSuccess`
- [ ] `maxBudgetUsd` is set on every `query()` call
- [ ] `UserPromptSubmit` hook persists user message as episode
- [ ] `PreToolUse` hook enforces privacy filter on `store_memory` and returns correct deny format
- [ ] `PreCompact` hook reads transcript from `transcript_path`, archives assistant messages as
  episodes
- [ ] `PostCompact` hook records the `compact_summary` in a `session.compacted` event
- [ ] `Stop` hook records the assistant's last message as an episode
- [ ] All hooks wrapped in `safeHook()` — failures logged, never crash the engine
- [ ] `message.received` event and episode serve distinct purposes (audit vs memory)
- [ ] Errors return as values, never crash the engine
- [ ] System prompt ordered stable→volatile for cache efficiency
- [ ] Active skills retrieved by trigger similarity and included in system prompt
- [ ] Session manager uses topic continuity (embedding similarity) to extend timed-out sessions
- [ ] Session depth tracking extends effective timeout for deep sessions
- [ ] `session_management` self-model domain tracks session decision accuracy
- [ ] `just check` passes

## Test Cases

### `tests/chat/context.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Full assembly | All memory tiers populated | Prompt contains all sections with headers |
| Empty memory | Fresh DB | Throws "System prompt too short" error |
| Minimal memory | Only persona and goals set | Prompt passes guard, has headers but sparse content |
| Budget capping | User model with 100 dimensions | Dimensions truncated to budget |
| RRF results included | Matching memories exist | Memories appear in prompt with scores |
| Skills in prompt | Skills exist matching query | Skills appear in prompt |
| No skills | No matching skills | Skills section omitted |

### `tests/chat/session.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| First message | No active session | New session created, ULID returned |
| Follow-up | Within timeout | Same session ID reused |
| Timeout | After inactivity window | New session created |
| Core memory change | Hash differs | New session created |
| Explicit release | `releaseSession()` called | Returns released ID, clears state |
| Activity recording | `recordActivity()` called | Extends timeout window |
| Topic continuity | Timed out but similar embedding | Session continues |
| Topic discontinuity | Timed out, dissimilar embedding | New session created |
| Deep session timeout | 50+ turns, slightly past timeout | Session continues (depth extends) |

### `tests/chat/engine.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Successful turn | Mock SDK yields result with subtype "success" | `turn.completed` event with token counts and cost |
| Failed turn | Mock SDK yields result with subtype "error_during_execution" | `turn.failed` event, `{ ok: false }` returned |
| Message event | Any message | `message.received` event emitted first |
| Session management | Two messages within timeout | Session reused for second |
| Session timeout | Two messages separated by timeout | New session for second |
| Streaming | Mock SDK yields stream_event messages | Ephemeral `stream.chunk` events emitted |
| Token extraction | Successful result | `inputTokens`, `outputTokens`, `costUsd` from result |
| Budget set | Any message | `maxBudgetUsd` present in query options |
| System prompt guard | Empty memory DB | Error returned, not crash |

### `tests/chat/hooks.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| UserPromptSubmit | Hook called with prompt | Episode created with role "user" |
| PreToolUse allow | Normal content on `store_memory` | Returns empty object (proceed) |
| PreToolUse deny | Sensitive content on `store_memory` | Returns `{ hookSpecificOutput: { permissionDecision: "deny", ... } }` |
| PreToolUse skip | Different tool name | Hook not invoked (matcher filters) |
| PreCompact | Transcript file exists | Assistant messages archived as episodes |
| PreCompact no transcript | `transcript_path` is undefined | No crash, no episodes |
| PostCompact | Hook called with summary | `session.compacted` event with `compact_summary` |
| Stop with message | `last_assistant_message` present | Episode created with role "assistant" |
| Stop without message | `last_assistant_message` undefined | No episode, no crash |
| Hook crash | Hook callback throws | Error logged via `hook.failed` event, turn continues |

## Risks

**High risk.** This is the most complex integration phase.

1. **SDK subprocess model.** The Agent SDK runs Claude as a subprocess. Environment variables must
   be in `process.env`. The `ANTHROPIC_API_KEY` must be available to the child process.

2. **Hook crash recovery.** If a hook throws, it could disrupt the agent loop. The `safeHook()`
   wrapper catches all exceptions, emits a `hook.failed` event, and returns an empty result so the
   turn continues.

3. **Async generator consumption.** The SDK returns an async generator from `query()`. Dropping the
   generator without consuming it leaks the subprocess. The `for await` loop in `handleMessage()`
   must consume fully. If an error occurs mid-stream, call `generator.return()` to clean up.

4. **Session resume.** Resuming a session uses the baked-in system prompt from when the session
   started. If core memory changed, the resumed session has stale context. This is why
   `shouldStartNewSession()` checks the core memory hash.

5. **Empty system prompt.** `settingSources: []` means NO default system prompt. If
   `assembleSystemPrompt()` returns empty, the agent has no instructions. The length guard (minimum
   50 characters) prevents this — it throws before `query()` is called.

6. **Transcript format.** The `PreCompact` hook reads the transcript from a file path. The SDK's
   transcript format may change between versions. `parseTranscript()` must handle unknown formats
   gracefully (skip unparseable lines, never crash).

7. **Budget enforcement.** `maxBudgetUsd` prevents runaway costs, but if set too low, the agent
   cannot complete complex tasks. The default ($0.50) is a starting point — tune based on observed
   usage in `turn.completed` events.

**Mitigations:**

- Start with the simplest possible `query()` call (no hooks, no session resume)
- Add hooks one at a time, verifying each against the real SDK types
- Add streaming second, after the basic flow works with complete responses
- Add session management last
- Test with mock SDK responses before real API calls
- Log every `SDKResultMessage` subtype to understand error conditions
