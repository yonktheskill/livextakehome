from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.connection import change
from app.core.config import Settings
from app.db.models import Action, Conversation, Message, Run, User, db
from app.db.repository import accept
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "timezone.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def register(client, email="timezone@example.com"):
    credentials = {"email": email, "password": "timezone-password-123"}
    assert client.post("/api/auth/register", json=credentials).status_code == 200
    return credentials


def test_timezone_requires_auth_valid_zone_and_same_origin(client):
    assert client.patch("/api/auth/me", json={"time_zone": "UTC"}).status_code == 401
    register(client)
    for zone in ["Mars/Olympus", "", "../UTC", "/etc/passwd", "UTC\x00"]:
        assert client.patch("/api/auth/me", json={"time_zone": zone}).status_code == 422
    assert (
        client.patch(
            "/api/auth/me",
            json={"time_zone": "Asia/Singapore"},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/auth/me").json()["time_zone"] == "UTC"


def test_timezone_persists_is_account_scoped_and_reaches_scheduling(client):
    credentials = register(client)
    updated = client.patch("/api/auth/me", json={"time_zone": "Asia/Kathmandu"})
    assert updated.status_code == 200
    assert updated.json()["time_zone"] == "Asia/Kathmandu"
    assert client.post("/api/auth/logout").status_code == 204
    register(client, "other@example.com")
    assert client.get("/api/auth/me").json()["time_zone"] == "UTC"
    client.post("/api/auth/login", json=credentials)
    assert client.get("/api/auth/me").json()["time_zone"] == "Asia/Kathmandu"
    with db.connection_context():
        # Simulate a request that read its user before the timezone correction committed.
        stale_user = User.get(User.email == credentials["email"])
        stale_user.time_zone = "UTC"
        accepted = accept(stale_user, "Find a meeting tomorrow", str(uuid4()))
        assert Message.get_by_id(accepted["message_id"]).time_zone == "Asia/Kathmandu"
        from app.agent.context import assemble

        assert any(
            '"time_zone":"Asia/Kathmandu"' in m["content"] for m in assemble(accepted["run_id"])
        )


def test_explicit_timezone_including_utc_survives_calcom_reconnection(client):
    register(client)
    identity = {"id": 42, "username": "owner", "timeZone": "Europe/London"}
    with db.connection_context():
        user = User.get()
        change(user.id, identity)
    assert client.get("/api/auth/me").json()["time_zone"] == "Europe/London"
    for zone in ["UTC", "Asia/Singapore"]:
        assert client.patch("/api/auth/me", json={"time_zone": zone}).status_code == 200
        with db.connection_context():
            change(user.id)
            change(user.id, identity)
        assert client.get("/api/auth/me").json()["time_zone"] == zone


def test_timezone_correction_invalidates_old_plans_but_preserves_dispatched_actions(client):
    register(client)
    with db.connection_context():
        user = User.get()
        conv = Conversation.get(Conversation.user == user)
        message = Message.create(conversation=conv, role="user", content="Book tomorrow")
        run = Run.create(
            conversation=conv, message=message, revision=conv.revision, status="running"
        )
        actions = {
            state: Action.create(
                run=run,
                revision=conv.revision,
                generation=0,
                kind="create_booking",
                inputs="{}",
                fingerprint=state,
                state=state,
            )
            for state in [
                "prepared",
                "authorized",
                "awaiting_confirmation",
                "dispatching",
                "succeeded",
            ]
        }
    assert client.patch("/api/auth/me", json={"time_zone": "America/New_York"}).status_code == 200
    with db.connection_context():
        assert Run.get_by_id(run.id).interrupted
        assert Conversation.get_by_id(conv.id).revision == conv.revision + 1
        for state, action in actions.items():
            expected = state if state in {"dispatching", "succeeded"} else "superseded"
            assert Action.get_by_id(action.id).state == expected
    # Saving the same timezone marks a manual preference without interrupting new work.
    with db.connection_context():
        accepted = accept(User.get(), "Find a slot", str(uuid4()))
    assert client.patch("/api/auth/me", json={"time_zone": "America/New_York"}).status_code == 200
    with db.connection_context():
        assert not Run.get_by_id(accepted["run_id"]).interrupted
