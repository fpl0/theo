# Decision Records

Architectural decisions are documented here. Each record covers one change: context, rationale, and files affected.

## Memory & Knowledge Graph

- [Node Operations](node-operations.md) — Knowledge graph nodes: kind, body, embedding, metadata
- [Edge Operations](edge-operations.md) — Graph edges with temporal validity and weight constraints
- [Episode Operations](episode-operations.md) — Conversation turn storage with embeddings
- [Core Memory Operations](core-memory-operations.md) — Protected identity documents (persona, goals, user_model, context)
- [Auto-edge Creation](auto-edge-creation.md) — Entity co-occurrence to edge auto-generation
- [Hybrid Retrieval](hybrid-retrieval.md) — Vector + FTS + graph BFS with RRF fusion
- [Memory Tool Integration](memory-tool-integration.md) — LLM-callable tools for knowledge graph
- [Contradiction Detection](contradiction-detection.md) — Conflicting facts flagged at storage time
- [Privacy Filter](privacy-filter.md) — PII detection, trust tiers, consent-based storage

## Learning & Models

- [Structured User Model](structured-user-model.md) — Psychological frameworks with confidence scores
- [Self Model](self-model.md) — Domain accuracy tracking
- [Onboarding Conversation](onboarding-conversation.md) — Structured user-model seeding flow

## Conversation & LLM

- [Anthropic LLM Client](anthropic-llm-client.md) — Streaming client with speed classification
- [Conversation Loop](conversation-loop.md) — Per-session locking and three-state lifecycle
- [Context Assembly](context-assembly.md) — Token budget estimation and context pipeline
- [Enhanced Context Assembly](enhanced-context-assembly.md) — Hybrid search integration and per-section budgets

## Infrastructure

- [Event Bus](event-bus-namespaces.md) — Persistent queue with typed events
- [Lifecycle Integration](lifecycle-integration.md) — Startup/shutdown orchestration
- [Graceful Degradation](graceful-degradation.md) — Circuit breaker, retry queue, health check
- [OpenObserve Provisioning](openobserve-provisioning.md) — Dashboard and alert setup
- [Dev Scripts](dev-scripts.md) — Justfile targets and automation

## Interface

- [Telegram Gate](telegram-gate.md) — aiogram 3.x with UUID5 sessions and streaming
- [Telegram Commands](telegram-commands.md) — Slash commands for system control
- [Voice Input](voice-input.md) — Speech-to-text via MLX Whisper
