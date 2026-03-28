# Changelog

All notable changes to Theo are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## M3 — 2026-03-28

### Added

- Voice message input via MLX Whisper — local transcription on Apple Silicon, no cloud APIs
- Privacy filter pipeline — trust tiers, content classification, PII detection at storage boundary
- Contradiction detection — conflicting facts flagged at storage time, confidence reduced
- Onboarding conversation — structured user-model seeding via multi-phase flow

## M2 — 2026-03-27

### Added

- Knowledge graph edges with temporal validity, weight constraints, and graph traversal
- Structured user model — dimensions across psychological frameworks with confidence scores
- Self-model — Theo tracks its own accuracy per domain
- Hybrid retrieval — vector + full-text + graph BFS fused via Reciprocal Rank Fusion
- Auto-edge creation from entity co-occurrence in conversations
- Enhanced context assembly with per-section token budgets and tiered eviction
- OpenObserve dashboard and alert provisioning via `just dashboards`

## M1 — 2026-03-26

### Added

- Event bus with persistent queue and typed events
- Memory package — node operations, episode operations, core memory (persona, goals, user_model, context)
- Anthropic LLM client with streaming and three speed tiers (reactive, reflective, deliberative)
- Context assembly with token budget estimation
- Conversation engine with per-session locking and streaming loop
- Memory tool integration — LLM-callable tools for knowledge graph operations
- Telegram gate with core messaging (aiogram 3.x, UUID5 sessions, MarkdownV2 escaping)
- Telegram slash commands — /start, /pause, /resume, /stop, /kill, /status
- Circuit breaker, retry queue, and health check (resilience package)
- Full system lifecycle orchestration in __main__.py
- Justfile with dev workflow commands
