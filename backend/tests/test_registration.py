import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import passwords, verify_password
from app.db.models import Conversation, Session, User, db
from app.main import create_app

CREDENTIALS = {"email": "testing@testingmail.com", "password": "testtesttest"}


def settings(tmp_path, **flags):
    return Settings(
        database_path=str(tmp_path / "auth.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
        **flags,
    )


def test_closed_registration_and_seeded_login_survive_restart(tmp_path):
    config = settings(tmp_path, registration_enabled=False, test_account_enabled=True)
    with TestClient(create_app(config)) as client:
        assert client.get("/api/auth/config").json() == {"registration_enabled": False}
        response = client.post("/api/auth/register", json=CREDENTIALS)
        assert response.status_code == 403
        assert response.json()["detail"] == "New registrations are currently disabled."
        assert "set-cookie" not in response.headers
        with db.connection_context():
            assert User.select().count() == Conversation.select().count() == 1
            assert Session.select().count() == 0
            user = User.get()
            original_id = user.id
            assert user.password_hash != CREDENTIALS["password"]
            assert verify_password(user.password_hash, CREDENTIALS["password"])
        assert client.post("/api/auth/login", json=CREDENTIALS).status_code == 200
        assert client.get("/api/chat/history").status_code == 200
        # A later password change must not be overwritten by startup seeding.
        with db.connection_context():
            User.update(password_hash=passwords.hash("changed-password")).execute()
    with TestClient(create_app(config)) as client:
        with db.connection_context():
            assert User.select().count() == Conversation.select().count() == 1
            assert User.get().id == original_id
        assert client.post("/api/auth/login", json=CREDENTIALS).status_code == 401
        assert (
            client.post(
                "/api/auth/login", json={**CREDENTIALS, "password": "changed-password"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_registration_flag_and_no_seed_when_disabled(tmp_path, enabled):
    config = settings(tmp_path, registration_enabled=enabled, test_account_enabled=False)
    with TestClient(create_app(config)) as client:
        assert client.get("/api/auth/config").json() == {"registration_enabled": enabled}
        with db.connection_context():
            assert User.select().count() == Conversation.select().count() == 0
        response = client.post("/api/auth/register", json=CREDENTIALS)
        assert response.status_code == (200 if enabled else 403)
        with db.connection_context():
            assert User.select().count() == Conversation.select().count() == int(enabled)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_account_flag_environment(monkeypatch, value, expected):
    monkeypatch.setenv("REGISTRATION_ENABLED", value)
    monkeypatch.setenv("TEST_ACCOUNT_ENABLED", value)
    config = Settings()
    assert config.registration_enabled is expected
    assert config.test_account_enabled is expected
