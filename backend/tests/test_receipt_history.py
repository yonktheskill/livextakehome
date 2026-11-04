import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.dispatch import settle
from app.db.models import Action, Connection, Event, Message, Run, User, db
from app.db.repository import accept, claim
from app.main import create_app
from app.tools.executor import Executor


@pytest.mark.parametrize("kind", ["create_booking", "reschedule_booking", "cancel_booking"])
def test_receipts_survive_reload_without_duplicate_json_and_are_owner_scoped(tmp_path, kind):
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
                kind,
                "{}",
                "fp",
                False,
                {"method": "POST", "path": "/v2/bookings", "payload": {"start": "..."}},
            )
            assert claim(action_id)
            settle(
                action_id, "succeeded", {"response": {"uid": "durable-uid", "status": "accepted"}}
            )
            run = Run.get_by_id(run_id)
            reply = Message.create(
                conversation=run.conversation_id,
                role="assistant",
                content="Your booking is confirmed.",
            )
        history = client.get("/api/chat/history").json()
        assert history["receipts"][0]["details"]["booking_uid"] == "durable-uid"
        assert history["receipts"][0]["kind"] == kind
        assert history["receipts"][0]["details"]["status"] == "accepted"
        assert [m["content"] for m in history["messages"]] == ["book", reply.content]
        assert "Action receipt" not in json.dumps(history["messages"])
        assert "```json" not in json.dumps(history["messages"])

        # The internal action message still anchors its card on older history pages.
        # Keep the source user message and the action on separate pages as well.
        with db.connection_context():
            anchor = Message.get(Message.client_request_id == f"action:{action_id}")
            Message.update(created_at=1).where(Message.id == run.message_id).execute()
            Message.update(created_at=2).where(Message.id == anchor.id).execute()
            Message.update(created_at=3).where(Message.id == reply.id).execute()
            Message.insert_many(
                [
                    {
                        "conversation": run.conversation_id,
                        "role": "assistant",
                        "content": f"Later message {i}",
                        "created_at": 100 + i,
                    }
                    for i in range(49)
                ]
                + [
                    {
                        "conversation": run.conversation_id,
                        "role": "assistant",
                        "content": f"Earlier message {i}",
                        "created_at": 1 + (i + 1) / 100,
                    }
                    for i in range(49)
                ]
            ).execute()
            event = Event.get(Event.kind == "action.result")
            assert json.loads(event.payload)["details"]["booking_uid"] == "durable-uid"
            assert "durable-uid" in anchor.content
        newest = client.get("/api/chat/history").json()
        assert newest["receipts"] == []
        older = client.get("/api/chat/history", params={"before": newest["next_cursor"]}).json()
        assert older["receipts"] == history["receipts"]
        assert len(older["messages"]) == 49
        assert all(m["content"].startswith("Earlier message") for m in older["messages"])
        oldest = client.get("/api/chat/history", params={"before": older["next_cursor"]}).json()
        assert [m["content"] for m in oldest["messages"]] == ["book"]
        assert oldest["next_cursor"] is None
        client.post(
            "/api/auth/register",
            json={"email": "other-history@example.com", "password": "long-test-password-123"},
        )
        assert client.get("/api/chat/history").json()["receipts"] == []
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        with db.connection_context():
            assert Action.get_by_id(action_id).state == "succeeded"
