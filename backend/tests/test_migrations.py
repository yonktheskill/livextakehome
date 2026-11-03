import time

from peewee import Model

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
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        assert {"logical_key", "request"}.issubset({c.name for c in db.get_columns("action")})
        assert Action.get_by_id(old_action.id).state == "superseded"
        assert Confirmation.select().count() == 1
        assert SchemaVersion.select().order_by(SchemaVersion.version.desc()).get().version == 2
    initialize(path)
