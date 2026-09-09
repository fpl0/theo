"""Smoke-test an installed wheel outside the checkout without network or model use."""

import asyncio
import importlib
import os
import pkgutil
import sys
import tempfile
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

import theo


async def main() -> None:
    os.environ["THEO_TEST_OFFLINE"] = "1"
    os.environ["THEO_TELEMETRY_ENABLED"] = "0"
    assert theo.__file__ is not None
    assert Path(theo.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), (
        "Smoke test must load Theo from an installed wheel, not the source checkout"
    )
    modules = [
        entry.name
        for entry in pkgutil.walk_packages(theo.__path__, prefix="theo.")
        if not entry.name.endswith(".__main__")
    ]
    for name in modules:
        importlib.import_module(name)

    from theo.cli.parser import parser
    from theo.observer import main as observer_main
    from theo.storage import Database

    entrypoints = distribution("theo-assistant").entry_points
    assert any(
        e.name == "theo" and e.value == "theo.cli:main" and callable(e.load()) for e in entrypoints
    )
    assert callable(observer_main)
    assert parser().parse_args(["status"]).command == "status"
    migrations = [
        p for p in files("theo").joinpath("migrations").iterdir() if p.name.endswith(".sql")
    ]
    assert len(migrations) >= 4
    with tempfile.TemporaryDirectory(prefix="theo-wheel-data-") as temporary:
        db = Database(Path(temporary))
        try:
            await db.initialize()
            applied = await db.read("SELECT version FROM schema_migrations")
            assert len(applied) == len(migrations)
        finally:
            await db.close()
    print(
        f"Installed wheel verified: {len(modules)} modules, CLI entry point, {len(migrations)} migrations"
    )


if __name__ == "__main__":
    asyncio.run(main())
