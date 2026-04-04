# Theo

**Personal agent with persistent memory, autonomous scheduling, and full event sourcing.**

Theo is not a chatbot with memory bolted on. It is a living system—one that accumulates knowledge, builds a model of its owner, acts autonomously, and remembers everything. Designed to run locally for a decade, it is built on Bun, TypeScript, the Claude Agent SDK, and PostgreSQL + pgvector.

---

## [+] Architecture Overview

Theo's architecture revolves around five core primitives. For an in-depth breakdown, please read the [Foundation Document](docs/foundation.md).

1. **Event Log:** An immutable, append-only record of everything that happens, backed by PostgreSQL. No state is ever silently dropped.
2. **Event Bus:** Unified with the event log, it dispatches events to in-memory handlers and guarantees delivery with durable checkpoints.
3. **Memory:** A multi-tier knowledge store featuring Graph nodes, Episodic transcripts, core behavior models, and Jungian-based user models. Queries are powered by Reciprocal Rank Fusion (vector + full-text + graph).
4. **Agent SDK:** The Claude Agent SDK handles the runtime loop, short-term session state, tools, and thinking logic.
5. **Scheduler:** Empowers Theo to act autonomously, waking up on a cron schedule to consolidate memory, reflect, or scan for forgotten commitments.

## [!] Current Status

Theo is currently in its initial **scaffolding phase**. The architecture is meticulously documented, and the foundation is laid out, but the implementation is intentionally in early stages to allow the structure to emerge cleanly.

- `docs/foundation.md` — The source of truth for the target architecture.
- `.claude/` — Engineering rules and workflows.
- `src/` — Contains the initial minimal runtime entry points (e.g., `index.ts`) and db migration scaffold.
- `tests/` — The canonical home for the test suite (test files are intentionally segregated from source files).

## [x] Stack & Tooling

Every dependency in Theo was chosen to be durable, strict, and performant:

- **Runtime:** [Bun](https://bun.sh)
- **Language:** TypeScript (`strict`)
- **Agent SDK:** `@anthropic-ai/claude-agent-sdk`
- **Database:** PostgreSQL (`postgres.js`) + `pgvector`
- **Quality Gates:** `biome` (formatting & linting), `zod` (validation)
- **Task Runner:** `just`

> Note: Bun is pinned via `packageManager` and `.tool-versions`.

## [>] Getting Started

### Prerequisites

- [Bun](https://bun.sh) >= 1.3.11
- [just](https://just.systems)

### Installation

```bash
bun install
```

### Running the Scaffold

```bash
bun run src/index.ts
```

### Development Commands

We rely on `just` as the stable command surface for local workflows:

```bash
# Quality Gates
just check      # Full quality gate: biome + tsc + test
just fmt        # Auto-format with biome
just lint       # Lint check with biome
just typecheck  # tsc --noEmit
just test       # Run tests via `bun test`

# Infrastructure & Execution
just up         # Start PostgreSQL (docker compose)
just down       # Stop containers
just migrate    # Run the migration runner 
just dev        # Start infra + agent
```
