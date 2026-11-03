import pytest
from cryptography.fernet import Fernet

from app.core.security import KeyVault
from app.db.database import initialize
from app.db.models import Connection, Conversation, User, db


@pytest.fixture
def database(tmp_path):
    initialize(str(tmp_path / "test.sqlite3"))
    with db.connection_context():
        yield


@pytest.fixture
def account(database):
    user = User.create(email="owner@example.com", password_hash="unused")
    Conversation.create(user=user)
    vault = KeyVault(Fernet.generate_key().decode())
    Connection.create(
        user=user,
        ciphertext=vault.seal("offline-key", user.id, 42),
        owner=42,
        username="owner",
        generation=1,
    )
    return user, vault
