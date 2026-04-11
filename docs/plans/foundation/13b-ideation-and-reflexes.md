# Phase 13b: Autonomous Ideation & Reflexes

## Motivation

Theo shouldn't just be reactive to goals you set; he should generate his own ideas
based on your context ("Thinking Space") and react to the world in real-time
("Reflexes").

This phase enables Theo to:

1. **Dream/Synthesize:** Background turns where he looks for intersections in your
   memory graph to propose new projects or insights.
2. **React to External Events:** Integrating with Webhooks (GitHub, Linear, Email)
   so Theo wakes up when *something happens* elsewhere.
3. **Proactive Proposals:** Theo creates a PR or drafts a message because he saw a
   need, not because he was asked.

## Depends on

- **Phase 12a** — Goal Loops (for executing the ideas)
- **Phase 13** — Background Intelligence (for the semantic connections)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/scheduler/ideation.ts` | "Thinking Space" job — synthesizing ideas from memory |
| `src/gates/webhooks.ts` | Webhook receiver for external events (GitHub, etc.) |
| `src/events/reflexes.ts` | Logic to map external events to specific goal/task triggers |
| `tests/scheduler/ideation.test.ts` | Idea generation and deduplication |
| `tests/gates/webhooks.test.ts` | Webhook parsing and event emission |

## Design Decisions

### Thinking Space (Dreaming)

A low-frequency, high-creativity job that uses the **Researcher** or **Reflector**
subagent to "look for the gaps."

```typescript
// Prompt for Ideation Job
const IDEATION_PROMPT = `
Review the owner's current User Model and recent Knowledge Graph nodes.
Find two unrelated nodes that have a potential intersection.
Propose a new project, insight, or automation that would be beneficial.
If the proposal is high-confidence, create a new Goal with priority 'low'.
`;
```

### External Reflexes

Instead of Theo polling GitHub, we expose a webhook endpoint.

1. **Webhook arrives** (e.g., GitHub PR opened).
2. **Reflex Handler** translates this to a `theo.event`.
3. **Executive Loop** sees the event. If the event matches an active Goal (e.g.,
   "Maintain project X"), it triggers an immediate "Thinking Turn" for that goal.

### Proactive Proposals (The "Draft" Pattern)

Theo should never just "do" something irreversible without asking, but he should
**"Draft and Notify."**

- **Pattern:** Create the change (in a branch or a draft) -> Send notification: "I
  noticed X, so I've drafted Y. Want to review?"
- This builds trust while maintaining the "always working" persona.

## Definition of Done

- [ ] `ideation` job runs successfully and occasionally creates new "Candidate Goals"
- [ ] Webhook endpoint accepts and validates external payloads (e.g., via HMAC for GitHub)
- [ ] Reflexes correctly trigger goal-related turns (e.g., a commit triggers a test run)
- [ ] Theo uses Model Tiering: Haiku for scanning webhooks, Sonnet/Opus for proposals
- [ ] Proactive code changes are always pushed to a feature branch with a PR description
- [ ] `just check` passes

## Test Cases

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Ideation Synthesis | Give Theo "Likes Rust" and "Building Finance App" | Proposal for "Rust-based finance CLI" |
| Webhook Trigger | Mock GitHub PR webhook | `job.triggered` for the relevant maintenance goal |
| Proposal Safety | Theo wants to change code | Change pushed to branch, notification sent |
