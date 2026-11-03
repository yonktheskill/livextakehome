import json
from uuid import uuid4

import pytest

from app.agent.context import assemble, retrieve
from app.agent.runner import Runner
from app.core.config import Settings
from app.db.database import query
from app.db.models import Event, Message, Run, Snapshot
from app.db.repository import accept
from app.providers.base import ProviderEvent
from app.providers.fake import FakeProvider


@pytest.mark.asyncio
async def test_runner_persists_chunks_before_replay_and_finishes(account):
    user, vault = account
    run_id = accept(user, "hello", str(uuid4()))["run_id"]
    runner = Runner(
        Settings(llm_provider="fake"),
        vault,
        FakeProvider(
            steps=[
                [
                    ProviderEvent("text_delta", text="Hello "),
                    ProviderEvent("text_delta", text="there"),
                    ProviderEvent("done"),
                ]
            ]
        ),
    )
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "succeeded"
    message = Message.get(Message.client_request_id == f"assistant:{run_id}")
    assert message.content == "Hello there" and message.status == "final"
    events = list(Event.select().where(Event.run == run_id).order_by(Event.sequence))
    assert [e.kind for e in events] == ["message.delta", "message.delta", "run.completed"]
    assert json.loads(events[0].payload)["message_id"] == message.id


@pytest.mark.asyncio
async def test_correction_interrupts_before_tool_dispatch(account):
    user, vault = account
    run_id = accept(user, "book", str(uuid4()))["run_id"]

    class CorrectingProvider:
        async def stream(self, messages, tools):
            await query(accept, user, "wait, Friday", str(uuid4()))
            yield ProviderEvent("tool_call", name="create_booking", arguments={}, call_id="1")

    runner = Runner(Settings(llm_provider="fake"), vault, CorrectingProvider())
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "interrupted"
    from app.db.models import Action

    assert Action.select().count() == 0


@pytest.mark.asyncio
async def test_provider_failure_keeps_partial_canonical_message(account):
    user, vault = account
    run_id = accept(user, "hello", str(uuid4()))["run_id"]

    class BrokenProvider:
        async def stream(self, messages, tools):
            yield ProviderEvent("text_delta", text="Partial")
            raise RuntimeError("private upstream error")

    runner = Runner(Settings(llm_provider="fake"), vault, BrokenProvider())
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "failed"
    assert Message.get(Message.client_request_id == f"assistant:{run_id}").status == "failed"
    assert "private upstream" not in "".join(e.payload for e in Event.select())


def test_context_compacts_and_retrieves_original_timestamp(account):
    user, _ = account
    run_id = None
    for index in range(65):
        run_id = accept(user, f"message {index} candidate Alice", str(uuid4()))["run_id"]
    context = assemble(run_id)
    assert Snapshot.select().count() == 1
    assert len(context) <= 24
    assert any("Canonical action evidence" in m["content"] for m in context)
    found = retrieve(run_id, "message 0")
    assert found and "created_at" in found[0] and "time_zone" in found[0]
