"""Peewee 3.x schema. All repository calls own their complete connection/transaction."""

import time
import uuid
from typing import TYPE_CHECKING

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

db = SqliteDatabase(None)


def uid():
    return str(uuid.uuid4())


class Base(Model):
    class Meta:
        database = db


class User(Base):
    id = AutoField()
    email = CharField(unique=True)
    preferred_name = CharField(null=True, max_length=200)
    password_hash = TextField()
    time_zone = CharField(default="UTC")
    time_zone_manual = BooleanField(default=False)
    created_at = FloatField(default=time.time)


class Session(Base):
    token_hash = CharField(primary_key=True)
    user = ForeignKeyField(User, on_delete="CASCADE")
    expires = FloatField()


class Connection(Base):
    user = ForeignKeyField(User, unique=True, on_delete="CASCADE")
    ciphertext = TextField(default="")
    owner = IntegerField(default=0)
    username = CharField(default="")
    generation = IntegerField(default=0)
    label = CharField(default="")


class Conversation(Base):
    user = ForeignKeyField(User, unique=True, on_delete="CASCADE")
    revision = IntegerField(default=0)


class Message(Base):
    id = CharField(primary_key=True, default=uid)
    conversation = ForeignKeyField(Conversation, on_delete="CASCADE")
    role = CharField()
    content = TextField(default="")
    created_at = FloatField(default=time.time, index=True)
    time_zone = CharField(default="UTC")
    status = CharField(default="final")
    client_request_id = CharField(null=True)

    class Meta:
        indexes = ((("conversation", "client_request_id"), True),)


class Run(Base):
    if TYPE_CHECKING:
        conversation_id: int
    id = CharField(primary_key=True, default=uid)
    conversation = ForeignKeyField(Conversation, on_delete="CASCADE")
    message = ForeignKeyField(Message)
    revision = IntegerField()
    status = CharField(default="queued", index=True)
    interrupted = BooleanField(default=False)
    created_at = FloatField(default=time.time)


class Event(Base):
    run = ForeignKeyField(Run, on_delete="CASCADE")
    sequence = IntegerField()
    kind = CharField()
    payload = TextField()
    created_at = FloatField(default=time.time)

    class Meta:
        indexes = ((("run", "sequence"), True),)


class Action(Base):
    id = CharField(primary_key=True, default=uid)
    run = ForeignKeyField(Run)
    revision = IntegerField()
    generation = IntegerField()
    kind = CharField()
    inputs = TextField()
    logical_key = CharField(null=True, unique=True)
    request = TextField(default="{}", null=True)
    fingerprint = CharField()
    state = CharField(default="prepared", index=True)
    result = TextField(default="{}")
    created_at = FloatField(default=time.time)


class Confirmation(Base):
    if TYPE_CHECKING:
        action_id: str
        evidence_message_id: str | None
    conversation = ForeignKeyField(Conversation)
    action = ForeignKeyField(Action, unique=True)
    expires = FloatField()
    evidence_message = ForeignKeyField(Message, null=True)
    consumed = BooleanField(default=False)


class Snapshot(Base):
    conversation = ForeignKeyField(Conversation)
    boundary = FloatField()
    summary = TextField()
    created_at = FloatField(default=time.time)


class Trace(Base):
    run = ForeignKeyField(Run)
    kind = CharField()
    payload = TextField()
    created_at = FloatField(default=time.time, index=True)


class SchemaVersion(Base):
    version = IntegerField(primary_key=True)


TABLES = [
    User,
    Session,
    Connection,
    Conversation,
    Message,
    Run,
    Event,
    Action,
    Confirmation,
    Snapshot,
    Trace,
    SchemaVersion,
]
