"""Database: asyncpg pool, schema, queries."""

from theo.db.pool import Database

db = Database()

__all__ = ["Database", "db"]
