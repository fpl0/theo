"""Asyncpg connection pool lifecycle."""

import logging

import asyncpg
from pgvector.asyncpg import register_vector

from theo.config import get_settings
from theo.errors import DatabaseNotConnectedError

log = logging.getLogger(__name__)


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await register_vector(conn)


class Database:
    """Owns the asyncpg connection pool lifecycle."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        """Return the live pool or raise."""
        if self._pool is None:
            raise DatabaseNotConnectedError
        return self._pool

    async def connect(self) -> None:
        """Create the connection pool and register pgvector codec."""
        cfg = get_settings()
        log.info("connecting pool (min=%d, max=%d)", cfg.db_pool_min, cfg.db_pool_max)
        self._pool = await asyncpg.create_pool(
            dsn=cfg.database_url.get_secret_value(),
            min_size=cfg.db_pool_min,
            max_size=cfg.db_pool_max,
            max_inactive_connection_lifetime=300.0,
            command_timeout=60.0,
            init=_init_connection,
            server_settings={"application_name": "theo", "statement_timeout": "30000"},
        )
        log.info("pool ready")

    async def close(self) -> None:
        """Gracefully close the pool."""
        if self._pool is not None:
            log.info("closing pool")
            await self._pool.close()
            self._pool = None
