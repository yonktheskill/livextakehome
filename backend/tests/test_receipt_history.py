from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.dispatch import settle
from app.db.models import Action, Connection, Run, User, db
from app.db.repository import accept, claim
from app.main import create_app
from app.tools.executor import Executor


def test_receipts_are_discoverable_after_reload_and_owner_scoped(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "history.db"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        owner = client.post(
            "/api/auth/register",
            json={"email": "history@example.com", "password": "long-test-password-123"},
        ).json()
        with db.connection_context():
            user = User.get_by_id(owner["id"])
            Connection.create(user=user, ciphertext="test-only", owner=42, generation=1)
            run_id = accept(user, "book", "receipt-test")["run_id"]
            action_id, _ = Executor.prepare_action(
                Run.get_by_id(run_id),
                Connection.get(Connection.user == user),
                "create_booking",
                "{}",
                "fp",
                False,
                {"method": "POST", "path": "/v2/bookings", "payload": {"start": "..."}},
            )
            assert claim(action_id)
            settle(
                action_id, "succeeded", {"response": {"uid": "durable-uid", "status": "accepted"}}
            )
        history = client.get("/api/chat/history").json()
        assert history["receipts"][0]["details"]["booking_uid"] == "durable-uid"
        assert any("durable-uid" in m["content"] for m in history["messages"])
        client.post(
            "/api/auth/register",
            json={"email": "other-history@example.com", "password": "long-test-password-123"},
        )
        assert client.get("/api/chat/history").json()["receipts"] == []
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        with db.connection_context():
            assert Action.get_by_id(action_id).state == "succeeded"
