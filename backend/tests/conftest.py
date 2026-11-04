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


def start_test_run(run_id):
    """Direct unit executors bypass the consumer, so explicitly acquire a run lease."""
    from app.db.models import Run

    Run.update(status="running").where(Run.id == run_id).execute()
