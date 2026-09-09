from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from theo.cli.commands import execute
from theo.cli.parser import parser
from theo.config import Settings, load_settings, save_settings
from theo.memory.context import ContextAssembler
from theo.storage import Database


async def test_init_finishes_database_after_configuration_only_startup(tmp_path):
    root = tmp_path / "partial-init"
    settings = Settings(owner_id="alice", timezone="America/New_York")
    save_settings(root, settings)
    result = await execute(parser().parse_args(["--data-root", str(root), "init"]))
    assert result["initialized"]
    assert result["existing_configuration_preserved"]
    assert (root / "theo.sqlite3").is_file()
    assert load_settings(root) == settings
    db = Database(root)
    try:
        assert await db.one("SELECT id,timezone FROM owners") == {
            "id": "alice",
            "timezone": "America/New_York",
        }
    finally:
        await db.close()


async def test_evaluate_locates_source_tests_after_package_reorganization(
    db, settings, monkeypatch
):
    save_settings(db.root, settings)
    spawn = AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(return_value=0)))
    monkeypatch.setattr("theo.cli.commands.asyncio.create_subprocess_exec", spawn)
    result = await execute(
        parser().parse_args(["--data-root", str(db.root), "evaluate", "--offline"])
    )
    assert result == {"exit_code": 0, "live_model_calls": 0}
    assert Path(spawn.call_args.args[-1]) == Path(__file__).resolve().parent
    assert spawn.call_args.kwargs["env"]["THEO_TEST_OFFLINE"] == "1"


async def test_repeated_init_does_not_claim_preserved_autonomy_is_paused(db, settings):
    save_settings(db.root, settings)
    await db.set_control("owner", "background_paused", "false")
    result = await execute(parser().parse_args(["--data-root", str(db.root), "init"]))
    assert result["initialized"]
    assert result["existing_configuration_preserved"]
    assert "autonomy" not in result
    assert "accounts" not in result
    assert await db.control("owner", "background_paused") == "false"


async def test_configured_timezone_reaches_context_and_survives_restart(
    db, settings, conversation, tmp_path
):
    save_settings(db.root, settings)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        settings.model_copy(update={"timezone": "America/New_York"}).model_dump_json()
    )
    await execute(
        parser().parse_args(["--data-root", str(db.root), "configure", "--file", str(replacement)])
    )
    assert (await db.one("SELECT timezone FROM owners WHERE id='owner'"))[
        "timezone"
    ] == "America/New_York"
    context = await ContextAssembler(db, "owner").assemble(conversation, "What is my time zone?")
    assert '"timezone":"America/New_York"' in context["rendered"]
    # Recover the canonical clock from the saved configuration even if an earlier
    # process stopped between replacing config.json and synchronizing the database.
    await db.execute("UPDATE owners SET timezone='Europe/Dublin' WHERE id='owner'")
    await execute(parser().parse_args(["--data-root", str(db.root), "status"]))
    assert (await db.one("SELECT timezone FROM owners WHERE id='owner'"))[
        "timezone"
    ] == "America/New_York"
