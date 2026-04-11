# Phase 14: Subagents, Onboarding & Engine Lifecycle

## Cross-cutting dependencies

Subagent definitions in this phase are consumed by Phase 12a (executive dispatch), Phase
13b (reflex dispatch, ideation), and Phase 10 (main chat delegation). Three
cross-cutting invariants apply:

1. **Advisor integration** (`foundation.md §4 Advisor-Assisted Execution`). Each
   `AgentDefinition` gains an optional `advisorModel?: string` field. Subagents whose
   work is plan-then-execute use `claude-sonnet-4-6` as executor + `claude-opus-4-6` as
   advisor:

   | Subagent | Executor | Advisor | Notes |
   | -------- | -------- | ------- | ----- |
   | `planner` | sonnet | opus | Canonical advisor use case |
   | `coder` | sonnet | opus | Downgraded from pure opus; big cost save |
   | `researcher` | sonnet | opus | Advisor suggests follow-ups |
   | `writer` | sonnet | opus | Voice checks on long drafts |
   | `reflector` | sonnet | opus | Meta perspective |
   | `psychologist` | opus | — | Already max |
   | `consolidator` | haiku | — | Mechanical |
   | `scanner` | haiku | — | Reflex-speed |

   The dispatch call site (Phase 10's chat engine, Phase 12a's executive loop, Phase
   13b's ideation job) passes `options.settings.advisorModel = subagent.advisorModel`
   when set and prepends the advisor timing block to the subagent's system prompt.

2. **Autonomy ladder integration** (`foundation.md §7.7`). The `self_model_domain` table
   from Phase 8 gains an `autonomy_level` column in Phase 12a's migration. The reflector
   subagent observes calibration per domain and recommends autonomy-level changes to the
   owner via notifications; the reflector **never auto-raises** a level, only recommends.
   The owner executes `/autonomy <domain> <level>` via the CLI gate.

3. **Skill creation with trust awareness.** When the reflector creates a skill via
   `store_skill`, the skill inherits the effective trust of the turn that triggered its
   creation. Skills created during external-tier reflex turns are capped at `external`
   trust and are filtered out of regular trigger-based retrieval.

## Motivation

This phase completes Theo. Subagents give Theo specialized cognitive modes -- a coder for
programming, a researcher for deep investigation, a psychologist for understanding the owner. The
onboarding flow makes the first interaction meaningful by building a foundational user model through
narrative conversation. The engine lifecycle ties everything together with proper startup, shutdown,
and state management.

This phase is the capstone. Subagents define Theo's cognitive modes (used by both conversations and
scheduled jobs). Onboarding uses subagents (psychologist). The engine lifecycle orchestrates
everything including subagent configuration. They compose into a single coherent phase because they
share initialization order dependencies.

After this phase, Theo is a complete personal AI agent: it talks, remembers, retrieves, schedules,
detects contradictions, self-organizes its knowledge graph, understands its owner, and manages its
own lifecycle.

## Depends on

- **Phase 10** -- Agent runtime (SDK integration, subagent configuration)
- **Phase 8** -- User model (onboarding populates it)
- **Phase 6** -- Core memory (onboarding seeds persona and goals)
- **Phase 12** -- Scheduler (subagents run as scheduled jobs)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/chat/subagents.ts` | All 8 subagent definitions with prompts and configuration |
| `src/chat/onboarding.ts` | First-interaction detection + psychologist-led conversation |
| `src/engine.ts` | `Engine` -- state machine, startup/shutdown, signal handling |
| `tests/chat/subagents.test.ts` | Subagent definition validation |
| `tests/chat/onboarding.test.ts` | Onboarding detection and flow |
| `tests/engine.test.ts` | State transitions, shutdown sequence |
| `src/memory/skills.ts` | `SkillRepository` — CRUD, trigger similarity search, version lineage |
| `tests/memory/skills.test.ts` | Skill creation, retrieval, versioning, promotion |

### Files to modify

| File | Change |
| ------ | -------- |
| `src/index.ts` | Use Engine for lifecycle management |

## Design Decisions

### Subagent Definitions

Subagent definitions use the SDK's `AgentDefinition` type. Tools are not specified -- subagents
inherit all tools from the parent agent, including `mcp__memory__*` tools:

```typescript
import type { AgentDefinition } from "@anthropic-ai/claude-agent-sdk";

const SUBAGENTS: Record<string, AgentDefinition> = {
  coder: {
    description: "Write, edit, and debug code across any language or framework",
    prompt:
      "You are Theo's software engineering agent. " +
      "You have full access to file system tools and " +
      "memory. Write clean, tested code. Follow the " +
      "owner's coding conventions. When debugging, " +
      "trace the problem systematically before " +
      "attempting fixes.",
    model: "opus",
    maxTurns: 200,
    // tools not specified → inherits all tools including mcp__memory__*
  },

  researcher: {
    description: "Deep investigation — web search, doc reading, synthesis",
    prompt:
      "You are Theo's research agent. Your job is " +
      "deep investigation. Search the web, read " +
      "documentation, cross-reference sources, and " +
      "synthesize findings into clear summaries. " +
      "Always cite your sources. Distinguish between " +
      "facts and opinions.",
    model: "sonnet",
    maxTurns: 50,
  },

  writer: {
    description: "Draft emails, messages, and documents in the owner's voice",
    prompt:
      "You are Theo's writing agent. Draft in the " +
      "owner's voice — check the user model for " +
      "their communication style. Match their tone, " +
      "formality level, and typical phrasing. When " +
      "drafting email replies, reference any relevant " +
      "context from memory.",
    model: "sonnet",
    maxTurns: 20,
  },

  planner: {
    description: "Break complex goals into concrete steps and identify dependencies",
    prompt:
      "You are Theo's planning agent. Break down " +
      "complex goals into concrete, actionable " +
      "steps. Identify dependencies between steps. " +
      "Estimate effort where possible. Store the " +
      "plan in memory so it can be tracked over time.",
    model: "sonnet",
    maxTurns: 20,
  },

  psychologist: {
    description: "Jungian psychologist — tracks psychological profile, behavioral patterns, individuation",
    prompt:
      "You are Theo's depth psychology agent, " +
      "grounded in Jungian analytical psychology. " +
      "You observe behavioral patterns, track " +
      "psychological dimensions (personality type, " +
      "shadow patterns, dominant archetypes, " +
      "individuation markers), and update the user " +
      "model with evidence-based observations. You " +
      "never diagnose — you observe patterns and " +
      "note them with appropriate confidence levels. " +
      "Your insights help Theo interact with the " +
      "owner in a way that supports their growth " +
      "and wellbeing.",
    model: "opus",
    maxTurns: 30,
  },

  consolidator: {
    description: "Compress and deduplicate memories",
    prompt:
      "You are Theo's memory maintenance agent. " +
      "Your job is to keep the knowledge graph " +
      "clean and efficient. Summarize old " +
      "conversations, merge duplicate knowledge " +
      "nodes, and ensure the graph doesn't grow " +
      "without bound. Preserve important nuance " +
      "when compressing — if in doubt, keep more " +
      "detail rather than less.",
    model: "haiku",
    maxTurns: 20,
  },

  reflector: {
    description: "Analyze behavioral patterns, calibrate self-model, and refine procedural skills",
    prompt:
      "You are Theo's self-reflection agent. " +
      "Review recent interactions and identify " +
      "patterns: What went well? What could " +
      "improve? Where was Theo's judgment accurate " +
      "vs inaccurate? Update the self-model " +
      "calibration for each domain. Look for " +
      "patterns the main agent might not notice — " +
      "recurring themes, blind spots, areas of " +
      "growth.\n\n" +
      "When you identify a recurring strategy that " +
      "works well, create or refine a procedural " +
      "skill using store_skill. When an existing " +
      "skill underperforms, create a refined version " +
      "with parent_id pointing to the predecessor. " +
      "Skills with success_rate > 0.85 and 20+ " +
      "attempts are candidates for promotion into " +
      "the persona.",
    model: "sonnet",
    maxTurns: 10,
  },

  scanner: {
    description: "Surface forgotten commitments and pending follow-ups",
    prompt:
      "You are Theo's proactive monitoring agent. " +
      "Search memory for any commitments, promises, " +
      "deadlines, or follow-ups that might have been " +
      "forgotten. Create notifications for anything " +
      "time-sensitive. Pay special attention to " +
      "phrases like \"I'll do X by Y\", " +
      "\"remind me\", \"don't forget\", and similar " +
      "commitment language.",
    model: "haiku",
    maxTurns: 10,
  },
};
```

Subagents are passed to the SDK via the `agents` configuration option. The main agent can delegate
to them during conversation. Because tools are not specified, subagents inherit the full tool set
from the parent -- this means every subagent has access to memory tools, which is the correct
behavior for an agent that needs to read/write Theo's memory.

### Onboarding

On first interaction, Theo detects an empty user model and launches the psychologist subagent with
an augmented system prompt.

#### Onboarding Detection

```typescript
async function shouldOnboard(userModel: UserModelRepository): Promise<boolean> {
  const dimensions = await userModel.getDimensions();
  return dimensions.length === 0;
}
```

#### Augmented System Prompt

When onboarding is detected, the system prompt is augmented with onboarding instructions. The
psychologist subagent is delegated to with this context:

```text
You are conducting Theo's onboarding interview. Proceed through three phases:

PHASE 1 — NARRATIVE (spend most time here):
Ask open-ended questions that invite stories. Key prompts:
- "Tell me about a turning point in your life..."
- "What's a challenge you're proud of overcoming?"
- "Describe a typical day when everything goes well."
After each answer, use store_memory to save key facts and observations.
When you have gathered enough narrative context (at least 4-5 exchanges),
transition to Phase 2 by saying: "I'd like to ask some more specific questions now."

PHASE 2 — STRUCTURED DIMENSIONS:
Ask targeted questions to fill these dimensions:
- Communication style: "Do you prefer direct feedback or diplomatic framing?"
- Energy patterns: "When's your peak productivity? Morning or evening?"
- Decision making: "Do you decide quickly on instinct, or do you deliberate?"
- Boundaries: "What topics are off-limits for me?"
- Interests: "What are you most passionate about right now?"
Use update_user_model for each dimension with confidence 0.6 (initial observation).
When all dimensions have been addressed, transition to Phase 3 by saying:
"Let's talk about how we'll work together."

PHASE 3 — WORKING AGREEMENT:
- "How autonomous should I be? When should I act vs. ask?"
- "What does helpful look like to you? What does annoying look like?"
- "Is there anything you want me to always remember?"
Use update_core to set the persona and goals slots based on the answers.
```

#### Onboarding Flow

```typescript
async function runOnboarding(chatEngine: ChatEngine, deps: OnboardingDeps): Promise<void> {
  // The onboarding augments the system prompt with the interview instructions above.
  // The psychologist subagent leads the conversation.
  // All memory operations use the standard MCP tools (store_memory, update_user_model, update_core).
  // The conversation is a normal session with normal episodes — nothing special about persistence.

  // The onboarding prompt is injected into the system prompt assembly (context.ts)
  // when shouldOnboard() returns true. The main agent sees the instructions and
  // delegates to the psychologist subagent automatically.
}
```

The onboarding is NOT a separate system -- it's a regular conversation with an augmented system
prompt that instructs the psychologist to lead the three-phase interview. All memory operations use
the standard MCP tools. The conversation is a normal session with normal episodes.

**What the onboarding produces:**

- Initial `user_model` dimensions with 0.6 confidence scores (initial observation)
- Initial `persona` and `goals` core memory slots
- Knowledge graph nodes for facts, preferences, values
- Behavioral baseline notes in episodic memory

### Skill Lifecycle

Skills follow a lifecycle: created → refined → promoted (optional).

**Creation**: The reflector subagent analyzes interaction patterns and creates skills via
`store_skill` MCP tool. The agent can also create skills explicitly during conversation.

**Refinement**: When a similar trigger already exists, a new version is created with `parent_id`
pointing to the predecessor. The old version remains for lineage tracking.

**Promotion**: When `success_rate` exceeds 0.85 with at least 20 attempts, the consolidation job
(Phase 13) can promote the skill — compiling its strategy into the persona. `promoted_at` is set,
excluding the skill from active retrieval.

**SkillRepository** methods: `create()`, `findByTrigger()`, `recordOutcome()`, `promote()`. All
mutations emit events through the bus.

The `store_skill` tool is added to `createMemoryServer()` alongside Phase 9 tools:

| `store_skill` | Create or refine a procedural skill | Autonomous |
| `search_skills` | Find relevant skills by trigger similarity | Autonomous |

### Engine Lifecycle

```typescript
type EngineState = "starting" | "running" | "paused" | "stopping" | "stopped";

interface EngineDependencies {
  readonly config: Config;
  readonly sql: Sql;
  readonly bus: EventBus;
  readonly scheduler: Scheduler;
  readonly gate: Gate;
  readonly chatEngine: ChatEngine;
  readonly userModel: UserModelRepository;
}

class Engine {
  private state: EngineState = "stopped";
  private stopping = false;
  private messageQueue: Array<{
    body: string;
    gate: string;
    resolve: (r: TurnResult) => void;
  }> = [];

  constructor(private readonly deps: EngineDependencies) {}

  async start(): Promise<void> {
    this.state = "starting";

    // 1. Run migrations
    await migrate(this.deps.sql);

    // 2. Start event bus (replay from checkpoints)
    await this.deps.bus.start();

    // 3. Start scheduler
    await this.deps.scheduler.start();

    // 4. Emit system.started
    await this.deps.bus.emit({
      type: "system.started",
      version: 1,
      actor: "system",
      data: { version: "0.1.0" },
      metadata: {},
    });

    // 5. Check if onboarding needed
    if (await shouldOnboard(this.deps.userModel)) {
      console.log("Welcome! Let's get to know each other...");
      // Onboarding will happen through the normal conversation flow
      // with an augmented system prompt
    }

    // 6. Start gate (begins accepting messages)
    await this.deps.gate.start();

    this.state = "running";
    this.stopping = false;
  }

  async stop(reason: string): Promise<void> {
    if (this.stopping) return; // prevent re-entry from signal race
    this.stopping = true;
    this.state = "stopping";

    // 1. Stop accepting new messages
    await this.deps.gate.stop();

    // 2. Stop scheduler (wait for in-flight jobs)
    await this.deps.scheduler.stop();

    // 3. Emit system.stopped
    await this.deps.bus.emit({
      type: "system.stopped",
      version: 1,
      actor: "system",
      data: { reason },
      metadata: {},
    });

    // 4. Stop bus (drain handlers)
    await this.deps.bus.stop();

    // 5. Close DB pool
    await this.deps.sql.end();

    this.state = "stopped";
  }

  async pause(): Promise<void> {
    this.state = "paused";
  }

  async resume(): Promise<void> {
    this.state = "running";
    // Drain queued messages in order
    while (this.messageQueue.length > 0) {
      const msg = this.messageQueue.shift()!;
      const result = await this.deps.chatEngine.handleMessage(msg.body, msg.gate);
      msg.resolve(result);
    }
  }

  /** Called by gates -- queues if paused, processes if running. */
  async handleMessage(body: string, gate: string): Promise<TurnResult> {
    if (this.state === "paused") {
      return new Promise((resolve) => {
        this.messageQueue.push({ body, gate, resolve });
      });
    }
    return this.deps.chatEngine.handleMessage(body, gate);
  }
}
```

#### Signal Handling

```typescript
// In index.ts
const engine = new Engine(deps);
await engine.start();

process.on("SIGTERM", () => engine.stop("SIGTERM"));
process.on("SIGINT", () => engine.stop("SIGINT"));
```

The `stopping` flag prevents double-stop when both SIGTERM and SIGINT arrive in quick succession, or
when a signal arrives during an already-in-progress shutdown.

## Definition of Done

- [ ] All 8 subagent definitions compile and use `AgentDefinition` type
- [ ] Subagents do not specify `tools` -- they inherit from parent (including MCP tools)
- [ ] Subagents are passed to SDK via agents configuration
- [ ] Main agent can delegate to subagents during conversation
- [ ] Onboarding detects empty user model via `getDimensions().length === 0`
- [ ] Onboarding conversation uses psychologist subagent with augmented prompt
- [ ] Augmented prompt covers three phases: narrative, structured dimensions, working agreement
- [ ] Onboarding seeds user model, core memory, and knowledge graph
- [ ] Engine constructor takes `EngineDependencies` object
- [ ] Engine starts: migrations --> bus replay --> scheduler --> gate
- [ ] Engine stops: gate --> scheduler --> bus --> pool (reverse order)
- [ ] `stopping` flag prevents double-stop from signal races
- [ ] Pause queues incoming messages; resume drains the queue
- [ ] SIGTERM triggers graceful shutdown
- [ ] SIGINT triggers graceful shutdown
- [ ] `system.started` and `system.stopped` events emitted
- [ ] `SkillRepository` supports create, findByTrigger, recordOutcome, promote
- [ ] Reflector subagent prompt includes skill creation/refinement instructions
- [ ] Skill version lineage tracked via parent_id
- [ ] `memory.skill.created` and `memory.skill.promoted` events emitted
- [ ] `just check` passes

## Test Cases

### `tests/chat/subagents.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| All defined | Check SUBAGENTS keys | All 8 names present |
| Valid models | Each subagent model | One of "opus", "sonnet", "haiku" |
| Max turns positive | Each subagent maxTurns | > 0 |
| Prompts non-empty | Each subagent prompt | Non-empty string |
| Descriptions non-empty | Each subagent description | Non-empty string |
| No tools specified | Each subagent | `tools` field is undefined (inherits from parent) |

### `tests/chat/onboarding.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Empty model triggers | No dimensions | `shouldOnboard()` = true |
| Populated model skips | Dimensions exist | `shouldOnboard()` = false |
| Onboarding uses psychologist | Onboarding triggered | Psychologist subagent invoked |
| Augmented prompt present | Onboarding active | System prompt contains three phase markers |

### `tests/engine.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Start sequence | Call start() | State transitions: stopped --> starting --> running |
| Stop sequence | Call stop() | State transitions: running --> stopping --> stopped |
| Start emits event | Engine starts | `system.started` event in log |
| Stop emits event | Engine stops | `system.stopped` event in log |
| SIGTERM shutdown | Send SIGTERM | Graceful stop |
| Double stop | Stop twice | Second call is no-op (stopping flag) |
| Stop before start | Stop without starting | Error or no-op |
| Pause queues | Pause then send message | Message queued, not processed |
| Resume drains | Resume after pause | Queued messages processed in order |

### `tests/memory/skills.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Create skill | Valid trigger + strategy | Skill with id, version=1 |
| Find by trigger | Similar trigger text | Returns matching skills ordered by similarity |
| Find excludes promoted | Promoted skill exists | Not returned by findByTrigger |
| Record success | recordOutcome(id, true) | success_count+1, attempt_count+1 |
| Record failure | recordOutcome(id, false) | attempt_count+1, success_count unchanged |
| Version lineage | Create skill with parentId | parent_id references predecessor |
| Promote | promote(id) | promoted_at set |

## Risks

**Low-medium risk.** Subagent definitions are mostly static configuration -- the prompts need tuning
but the structure is straightforward. The onboarding is a guided conversation that depends on prompt
quality, not complex engineering.

The engine lifecycle is the main engineering challenge -- ensuring the shutdown sequence is correct
(reverse of startup), that in-flight work is drained before closing resources, and that signal
handlers don't race with normal shutdown. The `stopping` flag and state machine prevent both
re-entry and invalid transitions.

The biggest risk is the onboarding prompt quality -- if the psychologist doesn't ask the right
questions or doesn't use the memory tools effectively, the onboarding produces a shallow user model.
This is a tuning problem that improves iteratively, not a Phase 14 blocker.
