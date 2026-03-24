"""Versioned schema migrations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from theo.db.pool import Database

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version     int         PRIMARY KEY,
    name        text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


async def migrate(db: Database) -> None:
    """Discover and apply unapplied migrations in order."""
    pool = db.pool

    with tracer.start_as_current_span("migrate"):
        async with pool.acquire() as conn:
            await conn.execute(_ENSURE_TABLE)

            rows = await conn.fetch("SELECT version FROM _schema_version")
            applied: set[int] = {row["version"] for row in rows}

            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", maxsplit=1)[0])
                if version in applied:
                    continue

                log.info("applying migration %04d: %s", version, path.stem)
                sql = path.read_text()

                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO _schema_version (version, name) VALUES ($1, $2)",
                        version,
                        path.stem,
                    )

            log.info("schema up to date")
