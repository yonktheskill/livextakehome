import pytest
from cryptography.fernet import Fernet

from app.core.security import KeyVault
from app.db.database import initialize
from app.db.models import Connection, Conversation, User, db


@pytest.fixture(autouse=True)
def account_flags(monkeypatch):
    # Deployment settings must not close registrations or seed accounts in unrelated tests.
    monkeypatch.setenv("REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("TEST_ACCOUNT_ENABLED", "false")


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


def start_test_run(run_id, *, resolve_inbox=True):
    """Acquire a lease for isolated unit tests, with semantic inbox out of scope.

    Request/integration tests opt out and exercise real semantic resolution tools.
    """
    import json

    from app.db.models import BookingRequest, RequestInbox, Run

    Run.update(status="running").where(Run.id == run_id).execute()
    if resolve_inbox:
        run = Run.get_by_id(run_id)
        RequestInbox.update(
            state="resolved",
            resolution=json.dumps({"input": [], "result": [], "legacy": True}),
            resolved_run_id=run_id,
        ).where(
            (RequestInbox.conversation == run.conversation_id) & (RequestInbox.state == "pending")
        ).execute()
        if (
            not BookingRequest.select()
            .where(BookingRequest.conversation == run.conversation_id)
            .exists()
        ):
            # A deterministic approval may already have resolved this legacy
            # fixture's inbox; retain that proof while keeping semantics out of scope.
            current = RequestInbox.get_or_none(RequestInbox.message == run.message_id)
            if current:
                resolution = json.loads(current.resolution)
                resolution["legacy"] = True
                current.resolution = json.dumps(resolution)
                current.save()
