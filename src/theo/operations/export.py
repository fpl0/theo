"""Export canonical owner data into portable JSON and Markdown projections.

Reads the database without modifying memory revisions or treating exported files
as authoritative state.
"""

import base64
import os
import sqlite3
import tempfile
from pathlib import Path

from theo import __version__
from theo.domain import encode
from theo.operations.backups import snapshot_database
from theo.storage import Database


async def export_data(db: Database, target: Path, format: str = "jsonl") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="theo-export-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        await snapshot_database(db, snapshot)
        connection = sqlite3.connect(snapshot)
        connection.row_factory = sqlite3.Row
        try:
            with target.open("w") as stream:
                if format == "markdown":
                    stream.write(
                        "# Theo memory export\n\nRead-only projection; JSONL or a backup preserves all structured history.\n\n"
                    )
                    for row in connection.execute(
                        "SELECT m.id,m.kind,m.status,r.version,r.body FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id ORDER BY m.id,r.version"
                    ):
                        stream.write(
                            f"## {row['id']} / revision {row['version']} / {row['status']}\n\n{row['body']}\n\n"
                        )
                else:
                    stream.write(
                        encode(
                            {
                                "type": "manifest",
                                "format": 1,
                                "application": __version__,
                                "external_blobs": "Use a full backup for original external media",
                            }
                        )
                        + "\n"
                    )
                    tables = [
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memory_fts%' ORDER BY name"
                        )
                    ]
                    for table in tables:
                        for row in connection.execute(
                            'SELECT * FROM "' + table.replace('"', '""') + '"'
                        ):
                            data = {
                                key: {"base64": base64.b64encode(value).decode()}
                                if isinstance(value, bytes)
                                else value
                                for key, value in dict(row).items()
                            }
                            stream.write(encode({"table": table, "record": data}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o600)
        finally:
            connection.close()
    return target
