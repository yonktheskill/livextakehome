import json
from datetime import UTC, datetime

import pytest
from conftest import start_test_run
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.dispatch import settle
from app.db.models import Action, Connection, Event, Message, Run, User, db
from app.db.repository import accept, claim, emit, finish
from app.main import create_app
from app.tools.executor import Executor


@pytest.mark.parametrize(
    "kind", ["create_booking", "create_event_type", "reschedule_booking", "cancel_booking"]
)
def test_action_results_survive_reload_in_their_correct_display_and_are_owner_scoped(
    tmp_path, kind
):
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
            start_test_run(run_id)
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
                action_id,
                "succeeded",
                {
                    "response": {"uid": "durable-uid", "status": "accepted", "title": "Intro"},
                    "validated": {"duration_minutes": 30, "location": "Cal Video"},
                },
            )
            run = Run.get_by_id(run_id)
            reply = Message.create(
                conversation=run.conversation_id,
                role="assistant",
                content="Your booking is confirmed.",
            )
            emit(run_id, "message.delta", {"message_id": reply.id, "text": reply.content})
            finish(run_id, "succeeded", "run.completed", {})
        history = client.get("/api/chat/history").json()
        replay = client.get(f"/api/runs/{run_id}/events").text
        payloads = [
            json.loads(line[6:]) for line in replay.splitlines() if line.startswith("data: ")
        ]
        streamed_messages = {p["message_id"]: p for p in payloads if "text" in p}
        for message in history["messages"]:
            if message["role"] == "assistant":
                assert streamed_messages[message["id"]]["created_at"] == message["created_at"]
        if kind == "create_booking":
            assert history["receipts"][0]["details"]["booking_uid"] == "durable-uid"
            assert history["receipts"][0]["kind"] == kind
            assert history["receipts"][0]["details"]["status"] == "accepted"
            assert next(p for p in payloads if "action_id" in p) == history["receipts"][0]
            with db.connection_context():
                anchor = Message.get(Message.client_request_id == f"action:{action_id}")
                assert history["receipts"][0]["message_id"] == anchor.id
                assert (
                    history["receipts"][0]["created_at"]
                    == datetime.fromtimestamp(anchor.created_at, UTC).isoformat()
                )
            assert [m["content"] for m in history["messages"]] == ["book", reply.content]
        else:
            assert history["receipts"] == []
            assert len(history["messages"]) == 3
            assert "“Intro”" in history["messages"][1]["content"]
            if kind == "create_event_type":
                assert "reusable 30-minute meeting type" in history["messages"][1]["content"]
            else:
                assert "**accepted**" in history["messages"][1]["content"]
        assert "Action receipt" not in json.dumps(history["messages"])
        assert "```json" not in json.dumps(history["messages"])

        # The action message anchors its result on older history pages.
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
            if kind == "create_booking":
                assert json.loads(event.payload) == history["receipts"][0]
            if kind != "create_booking":
                delta = Event.get(Event.kind == "message.delta")
                assert json.loads(delta.payload) == {
                    "message_id": anchor.id,
                    "text": anchor.content,
                }
                assert anchor.content == history["messages"][1]["content"]
                # Existing databases may still contain raw JSON action anchors.
                Message.update(content='Action receipt: ```json {"uid":"durable-uid"} ```').where(
                    Message.id == anchor.id
                ).execute()
        newest = client.get("/api/chat/history").json()
        assert newest["receipts"] == []
        older = client.get("/api/chat/history", params={"before": newest["next_cursor"]}).json()
        expected_receipts = [
            {**receipt, "created_at": "1970-01-01T00:00:02+00:00"}
            for receipt in history["receipts"]
        ]
        assert older["receipts"] == expected_receipts
        earlier_messages = older["messages"]
        if kind != "create_booking":
            assert earlier_messages[-1]["id"] == history["messages"][1]["id"]
            assert earlier_messages[-1]["content"] == history["messages"][1]["content"]
            earlier_messages = earlier_messages[:-1]
        assert len(earlier_messages) == 49
        assert all(m["content"].startswith("Earlier message") for m in earlier_messages)
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
