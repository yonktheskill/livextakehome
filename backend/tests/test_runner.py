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
from app.tools.executor import Waiting


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
    assert [e.kind for e in events] == ["message.delta", "run.completed"]
    assert json.loads(events[0].payload)["text"] == "Hello there"
    assert json.loads(events[0].payload)["message_id"] == message.id


@pytest.mark.asyncio
async def test_model_steps_have_paragraph_boundaries_and_keep_token_spacing(account):
    user, vault = account
    run_id = accept(user, "retry", str(uuid4()))["run_id"]
    provider = FakeProvider(
        steps=[
            [
                ProviderEvent("text_delta", text="I'll check"),
                ProviderEvent("text_delta", text="."),
                ProviderEvent(
                    "tool_call", name="search_history", arguments={"query": ""}, call_id="1"
                ),
            ],
            [
                ProviderEvent("text_delta", text=""),
                ProviderEvent("text_delta", text="See https://cal"),
                ProviderEvent("text_delta", text=".com, 12"),
                ProviderEvent("text_delta", text=".5 minutes"),
                ProviderEvent("text_delta", text="; hello"),
                ProviderEvent("text_delta", text=" there."),
            ],
        ]
    )
    await Runner(Settings(llm_provider="fake"), vault, provider).execute(run_id)
    message = Message.get(Message.client_request_id == f"assistant:{run_id}")
    assert message.content == "I'll check.\n\nSee https://cal.com, 12.5 minutes; hello there."
    replay = "".join(
        json.loads(event.payload)["text"]
        for event in Event.select()
        .where((Event.run == run_id) & (Event.kind == "message.delta"))
        .order_by(Event.sequence)
    )
    assert replay == message.content
    assert Run.get_by_id(run_id).status == "succeeded"


@pytest.mark.asyncio
async def test_confirmation_prompt_starts_a_new_paragraph(account):
    user, vault = account
    run_id = accept(user, "cancel", str(uuid4()))["run_id"]
    runner = Runner(
        Settings(llm_provider="fake"),
        vault,
        FakeProvider(
            steps=[
                [
                    ProviderEvent("text_delta", text="I found the booking."),
                    ProviderEvent(
                        "tool_call", name="cancel_booking", arguments={}, call_id="cancel"
                    ),
                ]
            ]
        ),
    )

    class ConfirmingExecutor:
        async def execute(self, run_id, name, arguments):
            raise Waiting("Confirm cancellation of this exact booking.")

    runner.executor = ConfirmingExecutor()
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "waiting_for_user"
    message = Message.get(Message.client_request_id == f"assistant:{run_id}")
    assert message.content == "I found the booking.\n\nConfirm cancellation of this exact booking."


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
async def test_provider_failure_does_not_publish_unchecked_partial_message(account):
    user, vault = account
    run_id = accept(user, "hello", str(uuid4()))["run_id"]

    class BrokenProvider:
        async def stream(self, messages, tools):
            yield ProviderEvent("text_delta", text="Partial")
            raise RuntimeError("private upstream error")

    runner = Runner(Settings(llm_provider="fake"), vault, BrokenProvider())
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "failed"
    assert not Message.select().where(Message.client_request_id == f"assistant:{run_id}").exists()
    assert not Event.select().where(Event.kind == "message.delta").exists()
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
