"""Optional local speech synthesis, transcription and video sampling.

Runs installed media tools with process deadlines and transcription with a
locally provisioned speech model. No hosted inference fallback is used.
"""

import asyncio
import importlib
import shutil
import sys
from pathlib import Path
from typing import Any

from theo.domain import Unavailable


async def run_media(command: list[str], timeout: int = 120) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()
        raise Unavailable("Media processing deadline exceeded") from None
    if process.returncode:
        raise Unavailable("Local media processor rejected the input")
    return raw


async def transcribe(path: Path, model_path: Path) -> str:
    if sys.platform != "darwin" or not model_path.exists():
        raise Unavailable(
            "Local speech model is not installed for this platform; original audio retained"
        )
    module: Any = importlib.import_module("mlx_whisper")
    result = await asyncio.to_thread(module.transcribe, str(path), path_or_hf_repo=str(model_path))
    return str(result["text"])


async def speak(text: str, destination: Path, voice: str | None = None) -> Path:
    if sys.platform != "darwin" or not shutil.which("say") or not shutil.which("ffmpeg"):
        raise Unavailable("Voice response requires local macOS say and FFmpeg")
    source = destination.with_suffix(".aiff")
    command = ["say", "-o", str(source)]
    if voice:
        command.extend(["-v", voice])
    # stdin avoids treating leading punctuation as command options.
    script = destination.with_suffix(".txt")
    script.write_text(text)
    try:
        await run_media([*command, "-f", str(script)])
        await run_media(
            ["ffmpeg", "-nostdin", "-y", "-i", str(source), "-c:a", "libopus", str(destination)]
        )
    finally:
        source.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
    return destination


async def video_keyframe(source: Path, destination: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise Unavailable("FFmpeg is not installed; original video retained")
    await run_media(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            str(destination),
        ]
    )
    return destination


async def video_samples(source: Path, directory: Path) -> list[tuple[float, Path]]:
    """Bounded, timestamped coverage; sample at most eight frames per video."""
    import json
    import math

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise Unavailable("Video samples require FFmpeg and ffprobe")
    raw = await run_media(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        ],
        timeout=15,
    )
    duration = float(json.loads(raw)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0 or duration > 7200:
        raise Unavailable("Video duration is unsupported or exceeds two hours")
    count = min(8, max(1, math.ceil(duration / 10)))
    result: list[tuple[float, Path]] = []
    for index in range(count):
        timestamp = duration * (index + 0.5) / count
        target = directory / f"sample-{index}.jpg"
        await run_media(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-ss",
                str(timestamp),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:-2",
                str(target),
            ],
            timeout=15,
        )
        result.append((timestamp, target))
    return result
