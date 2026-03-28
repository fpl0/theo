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

Most AI assistants forget everything between sessions. Theo doesn't. It maintains a knowledge graph that grows with every conversation — facts, relationships, and episodes searchable via vector similarity, full-text, and graph traversal, fused through Reciprocal Rank Fusion. Embeddings and speech-to-text run on-device via MLX; reasoning goes through the Claude API.

## Stack

- **Language**: Python 3.14+, async-native
- **Database**: PostgreSQL 18 + pgvector (asyncpg, raw SQL)
- **LLM**: Claude API (Anthropic)
- **Local inference**: MLX — embeddings (BGE-base) + speech-to-text (Whisper)
- **Interface**: Telegram (aiogram 3.x)
- **Observability**: OpenTelemetry → OpenObserve

## Decisions

Architectural decisions are documented in [`decisions/`](decisions/).
