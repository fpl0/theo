# Phase 4: Prompt Optimization

## Motivation

A 9B-parameter model is not Claude Sonnet. It has less capacity for implicit instruction
following, weaker tool-use reasoning, and a smaller effective context window (even if the nominal
window is 256K, quality degrades with length). The system prompt assembled by `context.ts` is
optimized for Claude -- verbose, nuanced, relying on Claude's strong instruction following. For
offline mode, the prompt needs to be shorter, more structured, and more explicit about tool use
expectations.

## Depends on

- **Phase 3** -- Engine knows the runtime mode
- **Foundation Phase 7.5** -- `buildPrompt()` exists in `src/chat/context.ts`

## Scope

### Files to modify

| File | Change |
| ------ | -------- |
| `src/chat/context.ts` | Mode-aware prompt assembly: shorter offline prompts, reduced memory budget, explicit tool guidance |

## Design Decisions

### Two Prompt Strategies, One Function

`assembleSystemPrompt()` already exists. Rather than creating a separate function for offline, we
add a `mode` parameter that adjusts token budgets and formatting:

```typescript
async function assembleSystemPrompt(
  deps: ContextDependencies,
  userMessage: string,
  mode: "online" | "offline",
): Promise<string> {
  const persona = await deps.coreMemory.readSlot("persona");
  const goals = await deps.coreMemory.readSlot("goals");
  const userModel = await deps.userModel.getDimensions();
  const context = await deps.coreMemory.readSlot("context");

  // Offline: fewer memories, smaller budget
  const memoryLimit = mode === "online" ? 15 : 8;
  const memories = await deps.retrieval.search(userMessage, { limit: memoryLimit });

  const skillLimit = mode === "online" ? 5 : 3;
  const skills = await deps.skills.findByTrigger(userMessage, skillLimit);

  const prompt = buildPrompt({
    persona,
    goals,
    userModel,
    context,
    memories,
    skills,
    mode,
  });

  if (prompt.length < 50) {
    throw new Error("System prompt too short -- memory may be empty. Run onboarding first.");
  }

  return prompt;
}
```

### Offline Prompt Format

The offline variant of `buildPrompt()` uses:

1. **Shorter persona section** -- Truncated at sentence boundary near 500 characters
2. **Bullet-point goals** -- Condensed, not narrative
3. **Explicit tool instructions** -- Small models need direct guidance on when and how to call
   tools. Without this, they tend to answer from their own knowledge rather than using memory
   tools.
4. **Fewer memories** -- 8 instead of 15. Less noise for a model that struggles with long context
5. **Fewer skills** -- 3 instead of 5. Reduce cognitive load

```typescript
/** Truncate text at the last sentence boundary before maxChars. */
function truncateAtSentence(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  const truncated = text.slice(0, maxChars);
  const lastPeriod = truncated.lastIndexOf(".");
  const lastNewline = truncated.lastIndexOf("\n");
  const boundary = Math.max(lastPeriod, lastNewline);
  if (boundary > maxChars * 0.5) return truncated.slice(0, boundary + 1);
  return truncated; // Fallback: hard cut if no good boundary found
}

function buildOfflinePrompt(sections: PromptSections): string {
  const parts: string[] = [];

  // Persona: condensed at sentence boundary
  if (sections.persona) {
    parts.push(
      `# Who You Are\n${truncateAtSentence(sections.persona, 500)}`,
    );
  }

  // Goals: condensed at sentence boundary
  if (sections.goals) {
    parts.push(
      `# Current Goals\n${truncateAtSentence(sections.goals, 300)}`,
    );
  }

  // Explicit tool use instructions for smaller models
  parts.push(`# Tool Use Rules
You have access to memory tools. ALWAYS use them:
- Use search_memory BEFORE answering factual questions about the user
- Use store_memory to save important new information
- Use read_core to check your persona, goals, and context
- Do NOT guess or make up information. Search memory first.
- When calling a tool, output ONLY the tool call, no extra text.`);

  // User model: brief
  if (sections.userModel.length > 0) {
    const brief = sections.userModel
      .slice(0, 5)
      .map((d) => `- ${d.name}: ${d.value}`)
      .join("\n");
    parts.push(`# About the User\n${brief}`);
  }

  // Memories: fewer, formatted clearly
  if (sections.memories.length > 0) {
    const formatted = sections.memories
      .map((m) => `- [${m.kind}] ${m.body}`)
      .join("\n");
    parts.push(`# Relevant Memories\n${formatted}`);
  }

  // Skills: fewer
  if (sections.skills.length > 0) {
    const formatted = sections.skills
      .map((s) => `- ${s.trigger}: ${s.procedure}`)
      .join("\n");
    parts.push(`# Skills\n${formatted}`);
  }

  return parts.join("\n\n");
}
```

### Why Not Reduce Tool Count

We expose the same MCP tools in both modes. Removing tools would require conditional MCP server
configuration, adding complexity. Instead, we guide the model's tool use through explicit prompt
instructions. If a specific tool is consistently misused in offline mode, it can be removed from
`allowedTools` in the engine (a one-line change in Phase 3's options).

### Token Budget

| Section | Online Budget | Offline Budget |
| ------- | ------------- | -------------- |
| Persona | Full | ~500 chars (sentence boundary) |
| Goals | Full | ~300 chars (sentence boundary) |
| User model | 10 dimensions | 5 dimensions |
| Memories | 15 results | 8 results |
| Skills | 5 results | 3 results |
| Tool instructions | Implicit | Explicit block (~200 chars) |

Total offline prompt should be roughly 2,000-3,000 tokens -- well within the effective attention
window of a 9B model, and well within the recommended `num_ctx` of 4096-8192 for bounded KV
cache.

## Definition of Done

- [ ] `assembleSystemPrompt()` accepts a `mode` parameter
- [ ] In offline mode: persona truncated at sentence boundary near 500 chars
- [ ] In offline mode: goals truncated at sentence boundary near 300 chars
- [ ] In offline mode: memory search limit is 8 (not 15)
- [ ] In offline mode: skill search limit is 3 (not 5)
- [ ] In offline mode: explicit tool use instructions are included in the prompt
- [ ] In offline mode: user model is limited to 5 dimensions
- [ ] `truncateAtSentence()` cuts at `.` or `\n`, not mid-word
- [ ] Online mode behavior is unchanged
- [ ] `just check` passes

## Test Cases

### `tests/chat/context.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Online prompt length | Full memory, `mode: "online"` | Prompt includes all 15 memories |
| Offline prompt length | Full memory, `mode: "offline"` | Prompt includes at most 8 memories |
| Offline tool instructions | `mode: "offline"` | Prompt contains "Tool Use Rules" |
| Sentence truncation | 600-char persona with period at 480 | Truncates at period, not at 500 |
| Sentence truncation fallback | 600-char persona, no period before 250 | Hard cut at 500 |
| Online persona full | Long persona, `mode: "online"` | Full text |
| Offline skill limit | 10 matching skills, `mode: "offline"` | Only 3 skills |

## Risks

**Medium risk.** Prompt engineering for small models is empirical. The token budgets and tool
instructions in this phase are a starting point. Real-world usage will reveal whether the model
follows the tool instructions, whether 8 memories is the right number, and whether the truncated
persona loses critical information. Expect iterative tuning after initial deployment.

The mitigation is that all budgets are constants, easy to adjust. No architectural changes are
needed to tune them.
