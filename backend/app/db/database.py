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
        if version and version.version > 6:
            raise RuntimeError("Database schema is newer than this application")
        if not version:
            db.create_tables(TABLES)
            SchemaVersion.create(version=6)
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

        if version and version.version < 5:
            from app.db.models import BookingRequest, RequestInbox

            db.create_tables([BookingRequest, RequestInbox])
            columns = {column.name for column in db.get_columns("action")}
            if "task_id" not in columns:
                db.execute_sql(
                    'ALTER TABLE "action" ADD COLUMN "task_id" VARCHAR(255) '
                    'REFERENCES "bookingrequest" ("id")'
                )
                db.execute_sql(
                    'CREATE INDEX IF NOT EXISTS "action_task_id" ON "action" ("task_id")'
                )
            if "task_revision" not in columns:
                db.execute_sql('ALTER TABLE "action" ADD COLUMN "task_revision" INTEGER')
            from app.db.requests import backfill_legacy_requests

            backfill_legacy_requests()
            SchemaVersion.create(version=5)

        if version and version.version < 6:
            from app.db.models import Attachment, Extraction

            db.create_tables([Attachment, Extraction])
            SchemaVersion.create(version=6)


async def query(fn, *args, **kwargs):
    """Offload a whole sync repository unit, never individual transaction statements."""

    def execute():
        with db.connection_context():
            return fn(*args, **kwargs)

    task = asyncio.create_task(asyncio.to_thread(execute))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker thread cannot be cancelled. Drain it before terminal bookkeeping
        # can inspect/invalidate the unit's writes, including repeated cancellation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # Observe below; preserve the original cancellation.
        # Observe failures too, while preserving the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise
