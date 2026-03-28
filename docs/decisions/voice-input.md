# Voice message input via Whisper

**Date:** 2026-03-28

## Context

Telegram supports voice messages (`.ogg` format). Users should be able to speak instead of type. M2 adds local transcription on Apple Silicon using MLX Whisper, keeping the local-first philosophy — no cloud transcription APIs, no data leaves the machine.

## Decisions

### MLX Whisper over cloud APIs

Rationale: Theo is local-first on Apple Silicon. Using `mlx-whisper` keeps audio data on-device, avoids API costs, and maintains the same dependency pattern as the embeddings module (`mlx` ecosystem). The `whisper-small` model balances accuracy and speed for conversational voice notes.

### Singleton pattern mirroring Embedder

Rationale: The `Transcriber` class uses the same double-checked locking, lazy-load, `asyncio.to_thread` pattern as `Embedder` in `embeddings.py`. This keeps the codebase consistent and avoids blocking the event loop during model loading or inference.

### mlx_whisper handles model caching internally

Rationale: Unlike the embeddings module which manually downloads and loads weights, `mlx_whisper.transcribe()` handles model download and caching internally via HuggingFace Hub. The `Transcriber._load()` method is therefore lightweight — it just records that initialization has been requested. The model path is passed on each `transcribe()` call.

### Voice and audio handled identically

Rationale: Telegram distinguishes `message.voice` (recorded in-app) from `message.audio` (forwarded audio files). Both are treated the same way — downloaded, transcribed, and published as `MessageReceived` events with `meta.source = "voice"`.

### Temp file cleanup in finally block

Rationale: Downloaded `.ogg` files are written to a temp path and cleaned up in a `finally` block to ensure no leaked files even if transcription fails.

## Files changed

- `src/theo/transcription.py` — new module: `Transcriber` singleton with async `transcribe()` API
- `src/theo/config.py` — added `whisper_model` setting (default: `mlx-community/whisper-small`)
- `src/theo/gates/telegram.py` — voice/audio message handling in `_on_message` and `_handle_voice`
- `src/theo/errors.py` — no changes (no new error types needed)
- `tests/conftest.py` — `mlx_whisper` stub for non-Apple Silicon test environments
- `tests/test_transcription.py` — new: transcriber unit tests
- `tests/test_telegram.py` — new: voice message handling tests
- `pyproject.toml` — added `mlx-whisper>=0.4` dependency
