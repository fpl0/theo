"""Enforce conversation visibility for canonical resources.

Resolves group scopes, labels created resources and denies cross-conversation
access. Unlabelled resources remain private to the owner.
"""

import sqlite3

from theo.domain import Denied
from theo.storage import Database


async def group_scope(db: Database, conversation: str) -> str | None:
    row = await db.one(
        "SELECT private FROM telegram_destinations WHERE conversation_id=?", (conversation,)
    )
    return conversation if row and not row["private"] else None


def visible_in(db: sqlite3.Connection, kind: str, resource: str, scope: str | None) -> bool:
    return (
        scope is None
        or db.execute(
            "SELECT 1 FROM resource_scopes WHERE kind=? AND resource_id=? AND conversation_id=?",
            (kind, resource, scope),
        ).fetchone()
        is not None
    )


def label_in(db: sqlite3.Connection, kind: str, resource: str, scope: str | None) -> None:
    if scope:
        db.execute("INSERT INTO resource_scopes VALUES(?,?,?)", (kind, resource, scope))


async def require_resource(db: Database, kind: str, resource: str, scope: str | None) -> None:
    if not await db.write(lambda connection: visible_in(connection, kind, resource, scope)):
        raise Denied("Resource is not available in this conversation")
