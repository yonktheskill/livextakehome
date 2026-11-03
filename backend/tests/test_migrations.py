from peewee import Model

from app.db.database import initialize
from app.db.models import Action, SchemaVersion, db


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
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=1)
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        assert {"logical_key", "request"}.issubset({c.name for c in db.get_columns("action")})
        assert SchemaVersion.select().order_by(SchemaVersion.version.desc()).get().version == 2
    initialize(path)
