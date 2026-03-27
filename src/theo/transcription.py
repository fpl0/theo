"""Local speech-to-text via MLX Whisper on Apple Silicon."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

import mlx_whisper
from opentelemetry import trace

from theo.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class Transcriber:
    """Thread-safe, lazily-loaded Whisper transcription model.

    Mirrors the :class:`~theo.embeddings.Embedder` singleton pattern:
    the model is loaded on first use and all public methods are async,
    running heavy MLX inference via ``asyncio.to_thread``.
    """

    def __init__(self) -> None:
        self._loaded = False
        self._lock = threading.Lock()

    # -- lazy loading (runs in worker thread) --------------------------------

    def _ensure_loaded(self) -> None:
        """Double-checked locking: fast path avoids the lock entirely."""
        if self._loaded:
            return

        with self._lock:
            if self._loaded:
                return
            self._load()

    def _load(self) -> None:
        model = get_settings().whisper_model
        log.info("loading whisper model %s", model)
        # mlx_whisper lazily downloads and caches the model on first
        # transcribe() call.  We just record that init has been requested.
        self._loaded = True
        log.info("whisper model ready: %s", model)

    # -- sync core (called inside worker thread) -----------------------------

    def _transcribe_sync(self, audio_path: str | Path) -> str:
        self._ensure_loaded()

        model = get_settings().whisper_model
        start = time.perf_counter()

        with tracer.start_as_current_span(
            "transcribe",
            attributes={"audio.path": str(audio_path)},
        ) as span:
            result: Mapping[str, object] = mlx_whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=model,
            )
            text = str(result.get("text", "")).strip()
            duration_s = time.perf_counter() - start

            span.set_attribute("transcription.duration_s", duration_s)
            span.set_attribute("transcription.text_length", len(text))

            log.info(
                "transcribed audio",
                extra={
                    "audio_path": str(audio_path),
                    "duration_s": round(duration_s, 2),
                    "text_length": len(text),
                },
            )

        return text

    # -- async public API ----------------------------------------------------

    async def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe an audio file and return the text."""
        return await asyncio.to_thread(self._transcribe_sync, audio_path)


# Module-level singleton — lazily loaded on first use.
transcriber = Transcriber()
