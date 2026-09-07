import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from rich.console import Console

from theo.backends.native import NativeBackend
from theo.cli import parser
from theo.delivery import Delivery
from theo.domain import Denied, ExecutionOutcome, Outcome
from theo.jobs import Jobs
from theo.runtime import Coordinator
from theo.terminal import (
    TerminalClient,
    TurnView,
    attachment_parts,
    extract_references,
    pasted_paths,
    render_turn,
    safe_text,
)
from theo.tools import ToolBroker


@pytest.fixture
async def terminal(db, settings):
    await db.execute(
        "INSERT INTO lifecycle_intervals VALUES(?,?,?,?,?,?)",
        ("daemon", "owner", db.clock(), None, db.clock(), 1),
    )
    client = TerminalClient(db, settings)
    await client.connect("test")
    await client.route("codex", "fixture-model")
    return client


def test_path_pastes_and_inline_references_preserve_prose(tmp_path):
    path = tmp_path / "my image.png"
    path.touch()
    assert pasted_paths(str(path)) == [path]
    assert pasted_paths("'" + str(path) + "'") == [path]
    assert pasted_paths(path.as_uri()) == [path]
    assert pasted_paths("Can you explain this?") == []
    text, paths = extract_references(f'''Explain @"{path}"; don't email me@example.com''')
    assert paths == [path]
    assert "don't email me@example.com" in text
    assert "[attached: my image.png]" in text


async def test_attachments_are_copied_extracted_and_owner_scoped(db, settings, tmp_path):
    note = tmp_path / "notes.md"
    note.write_text("Synthetic document content")
    photo = tmp_path / "image.png"
    Image.new("RGB", (8, 8), "blue").save(photo)
    parts = await attachment_parts(db, settings, [note, photo, note])
    assert len(parts) == 2
    assert parts[0]["text"] == "Synthetic document content"
    assert parts[1]["kind"] == "photo"
    note.unlink()
    artifact = await db.one(
        "SELECT owner_id,extracted_text FROM artifacts WHERE id=?", (parts[0]["artifact_id"],)
    )
    assert artifact == {"owner_id": "owner", "extracted_text": "Synthetic document content"}
    with pytest.raises(ValueError, match="regular file"):
        await attachment_parts(db, settings, [tmp_path])
    with pytest.raises(ValueError, match="at most"):
        await attachment_parts(db, settings, [tmp_path / str(n) for n in range(9)])
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="total limit"):
        await attachment_parts(
            db, settings.model_copy(update={"max_media_bytes": 1024}), [oversized]
        )


async def test_terminal_roundtrip_streams_then_waits_for_delivery(terminal, db, settings, tmp_path):
    reached = asyncio.Event()
    release = asyncio.Event()
    requests = []

    class Backend(NativeBackend):
        name = "fixture"

        async def execute(self, request, emit):
            requests.append(request)
            await emit("text_delta", {"text": "**Draft** response"})
            reached.set()
            await release.wait()
            return ExecutionOutcome(status=Outcome.COMPLETED, text="**Final** response")

    note = tmp_path / "note.md"
    note.write_text("Synthetic attachment")
    photo = tmp_path / "photo.png"
    Image.new("RGB", (8, 8), "blue").save(photo)
    job_id = await terminal.submit("Review my file", [note, photo])
    job = await Jobs(db, "owner").claim("interactive", "test")
    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "unused", factory=lambda _: Backend(db, settings)
    )
    task = asyncio.create_task(coordinator.run_job(job))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        # Emission and recording are separate tasks; yield until the recorded event appears.
        async with asyncio.timeout(5):
            while not (await terminal.view(job_id)).preview:
                await asyncio.sleep(0.01)
        view = await terminal.view(job_id)
        assert view.preview == "**Draft** response" and not view.done
        assert requests[0].parts[0].text == "Synthetic attachment"
        images = await Backend(db, settings).images(requests[0])
        assert len(images) == 1 and images[0]["mime"] == "image/jpeg"
        release.set()
        await task
        assert not (await terminal.view(job_id)).done
        sender = AsyncMock(return_value={"message_id": "local-receipt"})
        await Delivery(db, settings).dispatch_one(sender)
        view = await terminal.view(job_id)
        assert view.done and view.answer == "**Final** response" and view.delivery == "succeeded"
        assert sender.call_args.args[1]["_channel"] == "local"
        reopened = TerminalClient(db, settings)
        await reopened.connect("test")
        assert reopened.conversation == terminal.conversation
        assert (await reopened.history())[-1]["content"] == "**Final** response"
        await reopened.connect("other")
        assert await reopened.history() == []
        with pytest.raises(Denied):
            await reopened.view(job_id)
    finally:
        release.set()
        await task
        await broker.close()


async def test_terminal_stale_daemon_and_busy_conversation(terminal, db, clock):
    await terminal.submit("first", [])
    with pytest.raises(Denied, match="unfinished"):
        await terminal.submit("second", [])
    await terminal.cancel()
    assert (await terminal.view(terminal.last_job)).status == "cancelled"
    clock.advance(91)
    with pytest.raises(Denied, match="not responding"):
        await terminal.submit("third", [])
    assert (await db.one("SELECT count(*) n FROM jobs"))["n"] == 1


@pytest.mark.parametrize("external", [True, False])
async def test_terminal_cancellation_stops_daemon_worker(
    terminal, db, settings, tmp_path, external
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Backend(NativeBackend):
        name = "fixture"

        async def execute(self, request, emit):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    await terminal.submit("wait", [])
    job = await Jobs(db, "owner").claim("interactive", "test")
    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "unused", factory=lambda _: Backend(db, settings)
    )
    task = asyncio.create_task(coordinator.run_job(job))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if external:
            await terminal.cancel()
            await coordinator.reconcile_cancellations()
        else:
            await coordinator.cancel(job["id"])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert cancelled.is_set()
        assert not broker.tokens
        assert (await db.one("SELECT status FROM runs"))["status"] == "cancelled"
        assert (await db.one("SELECT count(*) n FROM actions"))["n"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await broker.close()


def test_terminal_rendering_and_cli_compatibility():
    output = io.StringIO()
    console = Console(file=output, width=80, color_system=None)
    console.print(
        render_turn(
            TurnView("completed", answer="# Result\n\n```python\nprint('Theo')\n```", done=True)
        )
    )
    assert "Result" in output.getvalue() and "print('Theo')" in output.getvalue()
    assert "\x1b" not in safe_text("\x1b[31munsafe\x07")
    assert parser().parse_args(["chat"]).text is None
    assert parser().parse_args(["chat", "hello"]).text == "hello"
    assert parser().parse_args(["chat", "--session", "work"]).session == "work"


async def test_real_prompt_session_exits_without_stopping_daemon(
    terminal, db, settings, monkeypatch
):
    from functools import partial

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import theo.terminal as ui

    output = io.StringIO()
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        ui, "Console", lambda **kwargs: Console(file=output, width=80, color_system=None)
    )
    with create_pipe_input() as pipe:
        monkeypatch.setattr(
            ui, "PromptSession", partial(PromptSession, input=pipe, output=DummyOutput())
        )
        pipe.send_text("/quit\n")
        await asyncio.wait_for(ui.interactive(db, settings, "test", None, None, []), 5)
    assert "Theo is still running" in output.getvalue()
    await terminal.ensure_running()


async def test_two_terminal_submissions_admit_only_one_turn(terminal, db):
    results = await asyncio.gather(
        terminal.submit("one", []), terminal.submit("two", []), return_exceptions=True
    )
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, Denied) for result in results) == 1
    assert (await db.one("SELECT count(*) n FROM jobs"))["n"] == 1
    assert (await db.one("SELECT count(*) n FROM messages WHERE role='user'"))["n"] == 1


async def test_bracketed_multiline_paste_is_submitted_as_one_message(
    terminal, db, settings, monkeypatch
):
    from functools import partial

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import theo.terminal as ui

    captured = []
    submitted = asyncio.Event()

    async def submit(self, text, paths):
        captured.append(text)
        submitted.set()
        return "synthetic-job"

    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui, "Console", lambda **kwargs: Console(file=io.StringIO(), width=80))
    monkeypatch.setattr(ui.TerminalClient, "submit", submit)
    monkeypatch.setattr(ui, "follow", AsyncMock())
    with create_pipe_input() as pipe:
        monkeypatch.setattr(
            ui, "PromptSession", partial(PromptSession, input=pipe, output=DummyOutput())
        )
        task = asyncio.create_task(ui.interactive(db, settings, "test", None, None, []))
        try:
            pipe.send_text("\x1b[200~Review this code:\nprint('Theo')\x1b[201~\n")
            await asyncio.wait_for(submitted.wait(), 5)
            pipe.send_text("/quit\n")
            await asyncio.wait_for(task, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert captured == ["Review this code:\nprint('Theo')"]
