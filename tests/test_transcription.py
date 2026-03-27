"""Tests for the transcription module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theo.transcription import Transcriber


class TestTranscriber:
    async def test_transcribe_returns_stripped_text(self) -> None:
        t = Transcriber()
        fake_result = {"text": "  hello world  "}
        with patch("theo.transcription.mlx_whisper.transcribe", return_value=fake_result):
            result = await t.transcribe("/tmp/test.ogg")
        assert result == "hello world"

    async def test_transcribe_returns_empty_on_missing_text(self) -> None:
        t = Transcriber()
        with patch("theo.transcription.mlx_whisper.transcribe", return_value={}):
            result = await t.transcribe("/tmp/test.ogg")
        assert result == ""

    async def test_transcribe_passes_model_and_path(self) -> None:
        t = Transcriber()
        with patch("theo.transcription.mlx_whisper.transcribe", return_value={"text": "ok"}) as m:
            await t.transcribe(Path("/tmp/test.ogg"))
        m.assert_called_once_with(
            "/tmp/test.ogg",
            path_or_hf_repo="mlx-community/whisper-small",
        )

    async def test_lazy_load_only_once(self) -> None:
        t = Transcriber()
        with patch("theo.transcription.mlx_whisper.transcribe", return_value={"text": "a"}):
            await t.transcribe("/tmp/a.ogg")
            await t.transcribe("/tmp/b.ogg")
        assert t._loaded

    async def test_singleton_exists(self) -> None:
        from theo.transcription import transcriber

        assert isinstance(transcriber, Transcriber)
