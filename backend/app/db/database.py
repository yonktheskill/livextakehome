import asyncio
from pathlib import Path

from app.db.models import TABLES, SchemaVersion, db


def initialize(path: str):
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db.init(
        path,
        pragmas={
            "journal_mode": "wal",
            "foreign_keys": 1,
            "busy_timeout": 10000,
            "synchronous": "full",
        },
    )
    with db.connection_context(), db.atomic("IMMEDIATE"):
        db.create_tables([SchemaVersion])
        version = SchemaVersion.select().order_by(SchemaVersion.version.desc()).first()
        if version and version.version > 1:
            raise RuntimeError("Database schema is newer than this application")
        if not version:
            db.create_tables(TABLES)
            SchemaVersion.create(version=1)


async def query(fn, *args, **kwargs):
    """Offload a whole sync repository unit, never individual transaction statements."""

    def execute():
        with db.connection_context():
            return fn(*args, **kwargs)

    return await asyncio.to_thread(execute)
