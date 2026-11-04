import asyncio
from pathlib import Path

from peewee import CharField, TextField
from playhouse.migrate import SqliteMigrator, migrate

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
        if version and version.version > 4:
            raise RuntimeError("Database schema is newer than this application")
        if not version:
            db.create_tables(TABLES)
            SchemaVersion.create(version=4)
        elif version.version == 1:
            migrator = SqliteMigrator(db)
            migrate(
                migrator.add_column("action", "logical_key", CharField(null=True)),
                migrator.add_column("action", "request", TextField(null=True)),
                migrator.add_index("action", ("logical_key",), unique=True),
            )
            from app.db.models import Action

            Action.update(request="{}").where(Action.request.is_null()).execute()
            # Old preparations lack frozen HTTP evidence and must be proposed again.
            Action.update(state="superseded").where(
                Action.state.in_(["prepared", "authorized", "awaiting_confirmation"])
            ).execute()
            SchemaVersion.create(version=2)
        if version and version.version < 3:
            migrate(
                SqliteMigrator(db).add_column(
                    "user", "preferred_name", CharField(null=True, max_length=200)
                )
            )
            SchemaVersion.create(version=3)
        if version and version.version < 4:
            # Native ADD COLUMN retains foreign keys; the migrator's NOT NULL
            # path rebuilds the user table, which has existing child records.
            db.execute_sql(
                'ALTER TABLE "user" ADD COLUMN "time_zone_manual" INTEGER NOT NULL DEFAULT 0'
            )
            SchemaVersion.create(version=4)


async def query(fn, *args, **kwargs):
    """Offload a whole sync repository unit, never individual transaction statements."""

    def execute():
        with db.connection_context():
            return fn(*args, **kwargs)

    return await asyncio.to_thread(execute)
