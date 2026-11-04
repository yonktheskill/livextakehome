import time

from peewee import Model
from playhouse.migrate import SqliteMigrator, migrate

from app.db.database import initialize
from app.db.models import Action, Confirmation, Conversation, Message, Run, SchemaVersion, User, db


def test_version_one_dispatch_evidence_upgrade(database):
    # Reconstruct the prior table through ORM schema APIs, no handwritten SQL.
    Action.drop_table()
    meta = type("Meta", (), {"database": db, "table_name": "action"})
    fields = {
        name: field.clone()
        for name, field in Action._meta.fields.items()
        if name not in {"logical_key", "request"}
    }
    legacy = type("LegacyAction", (Model,), {**fields, "Meta": meta})
    legacy.create_table()
    user = User.create(email="migration@example.com", password_hash="unused")
    conversation = Conversation.create(user=user, revision=1)
    message = Message.create(conversation=conversation, role="user", content="cancel")
    run = Run.create(conversation=conversation, message=message, revision=1)
    old_action = legacy.create(
        run=run,
        revision=1,
        generation=1,
        kind="cancel_booking",
        inputs='{"booking_uid":"abc"}',
        fingerprint="fp",
        state="awaiting_confirmation",
    )
    Confirmation.create(conversation=conversation, action=old_action.id, expires=time.time() + 600)
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=1)
    migrate(
        SqliteMigrator(db).drop_column("user", "preferred_name"),
        SqliteMigrator(db).drop_column("user", "time_zone_manual"),
    )
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        assert {"logical_key", "request"}.issubset({c.name for c in db.get_columns("action")})
        assert Action.get_by_id(old_action.id).state == "superseded"
        assert Confirmation.select().count() == 1
        assert SchemaVersion.select().order_by(SchemaVersion.version.desc()).get().version == 6
        assert User.get_by_id(user.id).preferred_name is None
        assert not User.get_by_id(user.id).time_zone_manual
    initialize(path)


def test_version_two_profile_upgrade_preserves_accounts(database):
    user = User.create(email="existing@example.com", password_hash="unchanged")
    migrate(
        SqliteMigrator(db).drop_column("user", "preferred_name"),
        SqliteMigrator(db).drop_column("user", "time_zone_manual"),
    )
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=2)
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        restored = User.get_by_id(user.id)
        assert restored.email == "existing@example.com"
        assert restored.password_hash == "unchanged"
        assert restored.preferred_name is None
        restored.preferred_name = "Alex"
        restored.save()
    initialize(path)
    with db.connection_context():
        assert User.get_by_id(user.id).preferred_name == "Alex"


def test_version_three_timezone_upgrade_preserves_profile(database):
    user = User.create(
        email="timezone@example.com",
        password_hash="unchanged",
        preferred_name="Alex",
        time_zone="Asia/Singapore",
    )
    migrate(SqliteMigrator(db).drop_column("user", "time_zone_manual"))
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=3)
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        restored = User.get_by_id(user.id)
        assert restored.time_zone == "Asia/Singapore"
        assert restored.preferred_name == "Alex"
        assert not restored.time_zone_manual
        restored.time_zone_manual = True
        restored.save()
    initialize(path)
    with db.connection_context():
        assert User.get_by_id(user.id).time_zone_manual
