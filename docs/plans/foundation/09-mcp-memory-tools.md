# Phase 9: MCP Memory Tools

## Motivation

The MCP memory tools are how the LLM controls Theo's memory. The agent decides what to remember,
what to search for, what to update. No hardcoded "always store the user's message" logic — the agent
has agency over its own memory.

These tools sit alongside the SDK's built-in tools (Read, Write, Bash, WebSearch, etc.), giving the
agent both memory operations and general capabilities. Each tool is thin: validate input with zod,
call the corresponding repository, emit events through the bus, return the result.

Without this phase, the agent has no way to interact with the memory system. With it, the SDK can
invoke `store_memory`, `search_memory`, `read_core`, etc. as part of its reasoning loop.

## Depends on

- **Phase 5** — Knowledge graph (nodes, edges)
- **Phase 6** — Episodic + core memory
- **Phase 7** — Hybrid retrieval (search)
- **Phase 8** — User model, self model, privacy filter

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/tools.ts` | MCP tool server with all memory tools |
| `tests/memory/tools.test.ts` | Tool input validation, event emission, integration |

## Design Decisions

### Tool Description Quality

Tool descriptions are the primary interface between the agent and the memory system. The LLM reads
them to decide *which* tool to call, *when* to call it, and *how* to interpret results. Bad
descriptions produce bad agent behavior — the agent misuses tools, calls the wrong one, or skips
memory operations entirely.

Guidelines for tool descriptions:

- **State when to use each tool.** "Use this when you learn something worth remembering" is
  actionable. "Stores a memory" is not.
- **State consequences.** "Changes are permanent and changelogged" prevents casual misuse of
  `update_core`.
- **Differentiate overlapping tools.** `store_memory` vs `update_user_model` — one is for discrete
  facts, the other for evolving behavioral dimensions.
- **Include boundary conditions.** What happens when search returns nothing? What does an empty core
  memory look like?
- **Keep them concise.** The LLM processes descriptions on every tool-use decision. Long
  descriptions waste tokens and dilute signal.

### MCP Server Setup

Using the Agent SDK's `createSdkMcpServer()` and `tool()`:

```typescript
import { createSdkMcpServer, tool } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

function createMemoryServer(deps: MemoryDependencies) {
  return createSdkMcpServer({
    name: "memory",
    tools: [
      storeMemoryTool(deps),
      searchMemoryTool(deps),
      searchSkillsTool(deps),       // NEW
      readCoreTool(deps),
      updateCoreTool(deps),
      linkMemoriesTool(deps),
      updateUserModelTool(deps),
      // Phase 12 adds: schedule_job, list_jobs, cancel_job
    ],
  });
}
```

The `tools` array holds `SdkMcpToolDefinition` values returned by `tool()`. Each tool definition is
a standalone function for testability and readability.

Phase 12 (Scheduler) will add `schedule_job`, `list_jobs`, and `cancel_job` to this same server. The
factory function will accept an extended dependencies interface at that point.

### Wiring into the Agent Runtime

Phase 10 consumes the server config returned by `createMemoryServer()`:

```typescript
const memoryServerConfig = createMemoryServer(deps);

const result = await query({
  prompt: body,
  options: {
    model: "claude-sonnet-4-6",
    mcpServers: { memory: memoryServerConfig },
    allowedTools: ["mcp__memory__*"],
    // ...
  },
});
```

The key `"memory"` in `mcpServers` becomes the namespace prefix — tools are accessible as
`mcp__memory__store_memory`, `mcp__memory__search_memory`, etc.

### Branded ID Helper

The knowledge graph uses branded `NodeId` types (defined in Phase 5's `src/memory/graph/types.ts`).
Tool handlers receive plain `number` from zod validation and need to convert. A single cast point
keeps `as` usage contained and auditable:

```typescript
// In src/memory/graph/types.ts (added in this phase if not already present)
function toNodeId(n: number): NodeId {
  return n as NodeId;
}
```

This is the only place `as NodeId` appears. All tool handlers use `toNodeId()` instead.

### Tool Definitions

#### `store_memory`

Create a knowledge graph node. The agent decides kind, body, sensitivity, and trust level.

```typescript
function storeMemoryTool(deps: MemoryDependencies) {
  return tool(
    "store_memory",
    "Store a new memory in the knowledge graph. " +
      "Use this when you learn something worth " +
      "remembering — facts about the user, their " +
      "preferences, observations, or beliefs. Choose " +
      "trust level based on source: use " +
      "owner_confirmed when the user directly states " +
      "something, inferred when you derive it from " +
      "context.",
    {
      kind: z.enum([
        "fact", "preference", "observation", "belief",
        "goal", "person", "place", "event",
        "pattern", "principle",
      ]),
      body: z.string().min(1).max(2000),
      sensitivity: z.enum(["normal", "financial", "medical", "identity", "location", "relationship"]).default("normal"),
      trust: z.enum(["owner_confirmed", "inferred", "external", "untrusted"]).default("inferred"),
    },
    async ({ kind, body, sensitivity, trust }) => {
      try {
        const node = await deps.nodes.create({
          kind,
          body,
          sensitivity,
          trust,
          actor: "theo",
        });
        return {
          content: [{ type: "text", text: `Stored memory #${node.id}: ${body.slice(0, 100)}` }],
        };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

The `trust` parameter defaults to `"inferred"` but can be elevated to `"owner_confirmed"` when the
user explicitly states something ("My name is...", "I prefer...", "I work at..."). The `"owner"` and
`"verified"` tiers are reserved for system-level operations and are not exposed to the tool.

#### `search_memory`

Hybrid RRF search across the knowledge graph.

```typescript
function searchMemoryTool(deps: MemoryDependencies) {
  return tool(
    "search_memory",
    "Search your memory for relevant knowledge. " +
      "Returns memories ranked by relevance using " +
      "vector similarity, keyword matching, and graph " +
      "connections. Use this before answering questions " +
      "that depend on what you know about the user, " +
      "or to check if you already know something " +
      "before storing a duplicate.",
    {
      query: z.string().min(1),
      limit: z.number().int().min(1).max(50).default(10),
      kinds: z.array(z.enum([
        "fact", "preference", "observation", "belief",
        "goal", "person", "place", "event",
        "pattern", "principle",
      ])).optional(),
    },
    async ({ query, limit, kinds }) => {
      try {
        const results = await deps.retrieval.search(query, { limit, kinds });
        const text = results
          .map((r) => `[#${r.node.id} ${r.node.kind}] (score: ${r.score.toFixed(3)}) ${r.node.body}`)
          .join("\n\n");
        return { content: [{ type: "text", text: text || "No memories found." }] };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

#### `read_core`

Read all four core memory slots.

```typescript
function readCoreTool(deps: MemoryDependencies) {
  return tool(
    "read_core",
    "Read your core memory — persona, goals, user " +
      "model summary, and current context. This is " +
      "your persistent identity and working state. " +
      "Core memory is assembled into your system " +
      "prompt at session start, but use this tool to " +
      "inspect the raw values or check for staleness.",
    {},
    async () => {
      try {
        const core = await deps.coreMemory.read();
        return { content: [{ type: "text", text: JSON.stringify(core, null, 2) }] };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

#### `update_core`

Update a core memory slot. Changelogged.

```typescript
function updateCoreTool(deps: MemoryDependencies) {
  return tool(
    "update_core",
    "Update a core memory slot. Use sparingly — " +
      "these define your identity, goals, and working " +
      "context. Every change is permanent and " +
      "changelogged. Prefer store_memory for ordinary " +
      "facts; reserve this for fundamental shifts in " +
      "persona, goals, user model summary, or current " +
      "context.",
    {
      slot: z.enum(["persona", "goals", "user_model", "context"]),
      body: z.record(z.unknown()),
    },
    async ({ slot, body }) => {
      try {
        await deps.coreMemory.update(slot, body, "theo");
        return { content: [{ type: "text", text: `Updated core memory: ${slot}` }] };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

#### `link_memories`

Create a labeled edge between two nodes.

```typescript
function linkMemoriesTool(deps: MemoryDependencies) {
  return tool(
    "link_memories",
    "Create a relationship between two memories. " +
      "Use to connect related concepts (relates_to), " +
      "mark contradictions (contradicts), build causal " +
      "chains (caused_by), or note supersession " +
      "(supersedes). Links strengthen retrieval — " +
      "connected memories surface together.",
    {
      sourceId: z.number().int(),
      targetId: z.number().int(),
      label: z.string().min(1),
      weight: z.number().min(0).max(5).default(1.0),
    },
    async ({ sourceId, targetId, label, weight }) => {
      try {
        await deps.edges.create({
          sourceId: toNodeId(sourceId),
          targetId: toNodeId(targetId),
          label,
          weight,
          actor: "theo",
        });
        return {
          content: [{ type: "text", text: `Linked #${sourceId} -> #${targetId} (${label})` }],
        };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

#### `update_user_model`

Update a user model dimension with evidence.

```typescript
function updateUserModelTool(deps: MemoryDependencies) {
  return tool(
    "update_user_model",
    "Update your understanding of the user along a " +
      "behavioral or psychological dimension. Unlike " +
      "store_memory (discrete facts), this tracks " +
      "evolving patterns — communication style, " +
      "technical depth, emotional tendencies. " +
      "Confidence grows with evidence count. Use when " +
      "you notice a recurring pattern, not for " +
      "one-off observations.",
    {
      dimension: z.string().min(1),
      value: z.record(z.unknown()),
      evidence: z.number().int().min(1).default(1),
    },
    async ({ dimension, value, evidence }) => {
      try {
        const dim = await deps.userModel.updateDimension(dimension, value, evidence, "theo");
        return {
          content: [{
            type: "text",
            text: `Updated ${dimension} ` +
              `(confidence: ${dim.confidence.toFixed(2)})`,
          }],
        };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

#### `search_skills`

Search procedural memory for relevant skills based on trigger similarity.

```typescript
function searchSkillsTool(deps: MemoryDependencies) {
  return tool(
    "search_skills",
    "Search your procedural memory for learned " +
      "strategies. Use when facing a task you might " +
      "have handled before — coding patterns, " +
      "communication approaches, problem-solving " +
      "methods. Returns skills ranked by trigger " +
      "similarity and success rate.",
    {
      query: z.string().min(1),
      limit: z.number().int().min(1).max(10).default(3),
    },
    async ({ query, limit }) => {
      try {
        const skills = await deps.skills.findByTrigger(query, limit);
        const text = skills
          .map((s) =>
            `[skill #${s.id}] ` +
            `(success: ${(s.successRate * 100).toFixed(0)}%` +
            `, v${s.version}) ${s.trigger}` +
            `\n  Strategy: ${s.strategy}`,
          )
          .join("\n\n");
        return { content: [{ type: "text", text: text || "No matching skills found." }] };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

### Error Handling

All tools return errors as values, never throw. A shared helper keeps the pattern consistent:

```typescript
import type { CallToolResult } from "@anthropic-ai/claude-agent-sdk";

function errorResult(error: unknown): CallToolResult {
  const message = error instanceof Error ? error.message : String(error);
  return { content: [{ type: "text", text: `Error: ${message}` }], isError: true };
}
```

The agent sees the error and adapts. No crashed tool calls.

### Dependencies Interface

```typescript
interface MemoryDependencies {
  readonly nodes: NodeRepository;
  readonly edges: EdgeRepository;
  readonly coreMemory: CoreMemoryRepository;
  readonly retrieval: RetrievalService;
  readonly userModel: UserModelRepository;
  readonly selfModel: SelfModelRepository;
  readonly skills: SkillRepository;      // NEW
}
```

This makes the tool server fully testable — inject mock dependencies.

## Definition of Done

- [ ] `createMemoryServer()` returns an MCP server config with all 7 tools
- [ ] `store_memory` validates input with zod, creates node with correct trust tier, returns
  confirmation
- [ ] `search_memory` runs RRF search, returns formatted results
- [ ] `read_core` returns all 4 core memory slots as JSON
- [ ] `update_core` updates slot, records changelog, emits event
- [ ] `link_memories` creates edge between nodes using `toNodeId()` (no raw `as` casts)
- [ ] `update_user_model` upserts dimension with evidence
- [ ] Invalid input returns zod error (not a crash)
- [ ] All tools return errors as values with `isError: true`
- [ ] Tool descriptions explain when to use, consequences, and differentiation from other tools
- [ ] `search_skills` finds skills by trigger similarity, returns formatted results with success
  rate and version
- [ ] `store_memory` accepts "pattern" and "principle" as valid kinds
- [ ] `just check` passes

## Test Cases

### `tests/memory/tools.test.ts`

| Test | Tool | Scenario | Expected |
| ------ | ------ | ---------- | ---------- |
| Store valid (inferred) | `store_memory` | Valid kind + body, default trust | Node created with trust "inferred" |
| Store valid (owner_confirmed) | `store_memory` | trust = "owner_confirmed" | Node created with trust "owner_confirmed" |
| Store invalid kind | `store_memory` | kind = "invalid" | Zod error returned |
| Store empty body | `store_memory` | body = "" | Zod error (min 1) |
| Search returns results | `search_memory` | Query matching nodes | Formatted result list |
| Search empty | `search_memory` | Query with no matches | "No memories found." |
| Search with kind filter | `search_memory` | kinds = ["fact", "preference"] | Only matching kinds returned |
| Read core | `read_core` | Fresh DB | JSON with 4 empty slots |
| Update core | `update_core` | Valid slot + body | Slot updated, changelog written |
| Update core invalid slot | `update_core` | slot = "invalid" | Zod error |
| Link valid | `link_memories` | Valid source + target | Edge created via toNodeId() |
| Link invalid IDs | `link_memories` | Non-existent node IDs | FK error returned as value |
| Update user model | `update_user_model` | Valid dimension | Dimension upserted |
| Error as value | any | Repository throws | `isError: true` response |
| Search skills returns results | `search_skills` | Query matching skill triggers | Formatted skill list with success rate |
| Search skills empty | `search_skills` | No matching skills | "No matching skills found." |
| Store pattern node | `store_memory` | kind = "pattern" | Node created |
| Store principle node | `store_memory` | kind = "principle" | Node created |
| Server has 7 tools | server | Inspect tools array | Length is 7, names match |

## Risks

**Low risk.** Tool handlers are thin adapters — all complexity is in the repositories (already
built). The main risk is getting the `tool()` parameter order right (name, description, inputSchema,
handler), which is verified against the SDK type definitions.

The tool descriptions matter for agent behavior — poorly described tools lead to misuse. The
descriptions above follow the quality guidelines: each states when to use the tool, what the
consequences are, and how it differs from related tools. These descriptions will need tuning based
on observed agent behavior in Phase 10 integration testing.
