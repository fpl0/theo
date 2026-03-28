# Theo

Autonomous personal agent with persistent episodic and semantic memory.
Built for decades of continuous use. Local-first on Apple Silicon.

```
User  ──▶  Telegram  ──▶  Conversation Engine  ──▶  Claude API
                                  │
                          Context Assembly
                           ╱     │     ╲
                     Core Mem  Hybrid   History
                     (identity) Search  (episodes)
                                │
                        Knowledge Graph
                      nodes · edges · BFS
```

## Why

Most AI assistants forget everything between sessions. Theo doesn't. It maintains a knowledge graph that grows with every conversation — facts, relationships, and episodes searchable via vector similarity, full-text, and graph traversal, fused through Reciprocal Rank Fusion. All inference (embeddings, speech-to-text) runs on-device via MLX. No data leaves your machine.

## Stack

Python 3.14+ · asyncpg · PostgreSQL 18 + pgvector · MLX · Claude API · aiogram 3.x · OpenTelemetry → OpenObserve

## Quick start

Prerequisites: [just](https://github.com/casey/just), [Docker](https://www.docker.com/products/docker-desktop/), [uv](https://docs.astral.sh/uv/)

```bash
just dev                      # start infra + dashboards + agent
```

Or step by step:

```bash
just up                       # start PostgreSQL + OpenObserve
just dashboards               # provision dashboards + alerts
just run                      # start the agent
just down                     # stop containers
```

| Service | URL | Credentials |
|---------|-----|-------------|
| OpenObserve | http://localhost:5080 | theo@theo.dev / theo |
| PostgreSQL | localhost:5432 | theo / theo / theo |

## Architecture

All code under `src/theo/`. Each module owns one concern.

| Module | Purpose |
|--------|---------|
| `conversation/` | Engine lifecycle, turn execution, context assembly |
| `memory/` | Knowledge graph — nodes, edges, episodes, retrieval, privacy, user model |
| `llm.py` | Anthropic streaming client, three speed tiers (reactive/reflective/deliberative) |
| `gates/telegram.py` | Telegram bot — commands, voice, streaming responses |
| `onboarding/` | Structured user-model seeding flow |
| `resilience/` | Circuit breaker, retry queue, health check |
| `db/` | asyncpg pool, forward-only SQL migrations |
| `bus/` | Persistent async event bus |
| `transcription.py` | Speech-to-text via MLX Whisper |
| `embeddings.py` | Local MLX embeddings (BGE-base, 768d) |
| `telemetry.py` | OpenTelemetry — traces, metrics, logs |

## Decisions

Architectural decisions are documented in [`docs/decisions/`](docs/decisions/). 24 records covering memory, conversation, infrastructure, and interface design.

## Development

```bash
just check                    # lint + typecheck + SQL lint + tests
just fmt                      # auto-format Python + SQL
just test                     # tests only
```

See [CLAUDE.md](CLAUDE.md) for conventions and quality gates.
