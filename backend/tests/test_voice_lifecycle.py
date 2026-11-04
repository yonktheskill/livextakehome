import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import start_test_run
from test_voice import delegation, session, transcript

from app.agent.runner import Runner
from app.api.chat import history
from app.core.config import Settings
from app.db import voice
from app.db.models import Action, Message, RequestInbox, Run, VoiceEvent, VoiceSession
from app.db.repository import emit, exposed
from app.providers.base import ProviderEvent
from app.providers.fake import FakeProvider
from app.providers.voice import VoiceConnection, VoiceManager
from app.tools.executor import Executor
from app.tools.schemas import definitions


def voice_run(user, text="Book tomorrow"):
    call = session(user)
    VoiceSession.update(status="active").where(VoiceSession.id == call.id).execute()
    voice.fragment(call.id, transcript("request", text, 100, 300))
    run_id, _ = voice.delegate(call.id, delegation())
    return call, Run.get_by_id(run_id)


@pytest.mark.asyncio
async def test_one_reply_live_and_after_history_reload(account, monkeypatch):
    user, _ = account
    call, run = voice_run(user)
    canonical = Message.create(
        conversation=run.conversation_id,
        role="assistant",
        source="voice_backend",
        content="What title should I use?",
        status="waiting_for_user",
        client_request_id=f"assistant:{run.id}",
    )
    linked = voice.reply(call.id, run.id)
    voice.fragment(
        call.id, transcript("answer", "What title should I use", 1000, 2200, "assistant")
    )
    voice.reply(call.id, run.id)
    assert Message.get_by_id(linked.id).content == "What title should I use"

    async def current(_):
        return user

    monkeypatch.setattr("app.api.chat.current", current)
    loaded = await history(SimpleNamespace())
    assistant = [m for m in loaded["messages"] if m["role"] == "assistant"]
    assert [(m["id"], m["content"]) for m in assistant] == [(linked.id, "What title should I use")]
    streamed = [
        json.loads(e.payload)["message"]["id"]
        for e in VoiceEvent.select().where(VoiceEvent.kind == "message.upsert")
    ]
    assert canonical.id not in streamed
    # End/reload must not re-expose the backend's duplicate waiting reply.
    voice.stop_work(call.id)
    voice.finalize(call.id)
    assert (
        len([m for m in (await history(SimpleNamespace()))["messages"] if m["role"] == "assistant"])
        == 1
    )


def test_exact_action_confirmation_is_attached_without_overwriting_evidence(account):
    user, _ = account
    call, run = voice_run(user, "Cancel that meeting")
    canonical = Message.create(
        conversation=run.conversation_id,
        role="assistant",
        source="voice_backend",
        content="Cancel the whole meeting? Reply yes.",
        status="waiting_for_user",
        client_request_id=f"assistant:{run.id}",
    )
    action = Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="cancel_booking",
        inputs="{}",
        fingerprint="cancel",
        state="awaiting_confirmation",
        request=json.dumps(
            {
                "confirmation_prompt": canonical.content,
                "confirmation_prompt_message_id": canonical.id,
                "confirmation_message_content": canonical.content,
            }
        ),
    )
    linked = voice.reply(call.id, run.id)
    voice.fragment(call.id, transcript("answer", "Shall I cancel it?", 1200, 2000, "assistant"))
    public = voice.public_message(Message.get_by_id(linked.id))
    assert public["content"] == "Shall I cancel it?"
    assert public["confirmation"] == {"message_id": canonical.id, "content": canonical.content}
    voice.finalize(call.id)
    voice.stop_work(call.id)
    assert exposed(action)
    assert Message.get_by_id(canonical.id).content == canonical.content
    assert not voice.visible_confirmation_ids(run.conversation_id)


@pytest.mark.parametrize(
    "text",
    [
        "End call",
        "Please hang up",
        "Could you end the call, please?",
        "Okay, end the call please.",
        "Goodbye",
        "I'm done",
        "Thanks, that's all.",
        "Okay, nice. End call",
        "All right, great, please end the call now.",
        "Um, okay, let's end the call.",
        "You can end the call now, thank you.",
        "Can we end this call please?",
    ],
)
def test_direct_hangup_intent(text):
    assert voice.wants_end_call(text)


@pytest.mark.parametrize(
    "text",
    [
        "Don't end the call",
        "What does end call mean?",
        "Use End Call as the meeting title",
        "Book tomorrow",
        "End the call after you book my meeting",
        "Bye for now is the title",
        "Okay, nice. Don't end the call",
        "Okay, nice. End call is the title of my meeting",
        'The attendee said "end call"',
        "Okay, end the call after you cancel Alice's meeting",
    ],
)
def test_hangup_phrases_inside_scheduling_or_negated_requests_do_not_disconnect(text):
    assert not voice.wants_end_call(text)


def test_auto_end_requires_a_real_booking_and_all_requests_settled(account):
    user, _ = account
    _, run = voice_run(user)
    start_test_run(run.id)
    Run.update(status="succeeded").where(Run.id == run.id).execute()
    assert not voice.booking_complete(run.id)

    action = Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="create_event_type",
        inputs="{}",
        fingerprint="created",
        state="succeeded",
    )
    assert not voice.booking_complete(run.id)
    action.kind = "create_booking"
    action.save()
    assert voice.booking_complete(run.id)
    RequestInbox.update(state="pending").where(RequestInbox.message == run.message_id).execute()
    assert not voice.booking_complete(run.id)


@pytest.mark.asyncio
async def test_new_speech_cancels_auto_end_even_during_farewell_dispatch(account):
    user, _ = account
    call, run = voice_run(user)
    start_test_run(run.id)
    Run.update(status="succeeded").where(Run.id == run.id).execute()
    Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="create_booking",
        inputs="{}",
        fingerprint="created",
        state="succeeded",
    )
    manager = VoiceManager(Settings())
    connection = VoiceConnection(manager, call.id)
    sending, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        sending.set()
        await release.wait()

    connection.send = send
    closing = asyncio.create_task(connection.request_end("booking_complete", "task", run.id))
    await sending.wait()
    voice.fragment(call.id, transcript("new-request", "Also book another meeting", 2000, 2400))
    continued = asyncio.create_task(connection.cancel_end())
    release.set()
    await asyncio.gather(closing, continued)
    assert connection.end_request is None
    kinds = [
        e.kind
        for e in VoiceEvent.select().order_by(VoiceEvent.sequence)
        if e.kind.startswith("voice.end_")
    ]
    assert kinds == ["voice.end_requested", "voice.end_cancelled"]
    assert not voice.booking_complete(run.id)
    await asyncio.gather(connection.end_task, return_exceptions=True)
    await manager.http.aclose()


def test_hangup_fallback_uses_the_latest_speech_not_an_old_unsubmitted_greeting(account):
    user, _ = account
    call = session(user)
    voice.fragment(call.id, transcript("greeting", "Hello there", 100, 300))
    voice.fragment(call.id, transcript("hangup", "End call", 3000, 3400))
    assert voice.end_call_request(call.id)


@pytest.mark.asyncio
async def test_agent_end_call_tool_is_voice_only_and_stops_later_tools(account):
    user, vault = account
    call, run = voice_run(user, "I need to leave now")
    start_test_run(run.id)
    provider = FakeProvider(
        steps=[
            [
                ProviderEvent("tool_call", call_id="hangup", name="end_call"),
                ProviderEvent("tool_call", call_id="bad-followup", name="create_booking"),
            ]
        ]
    )
    runner = Runner(Settings(llm_provider="fake"), vault, provider)
    await runner.execute(run.id)
    assert Run.get_by_id(run.id).status == "interrupted"
    assert not Action.select().exists()
    assert "end_call" in {t["function"]["name"] for t in provider.requests[0][1]}
    assert "end_call" not in {t["function"]["name"] for t in definitions()}
    assert VoiceEvent.select().where(VoiceEvent.session == call.id).exists()
    assert (await Executor(vault).execute(run.id, "end_call", {})).get("error")


@pytest.mark.asyncio
async def test_completed_booking_requests_end_but_waits_for_browser_playback(account, monkeypatch):
    user, _ = account
    call, run = voice_run(user)
    start_test_run(run.id)
    Run.update(status="succeeded").where(Run.id == run.id).execute()
    Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="create_booking",
        inputs="{}",
        fingerprint="created",
        state="succeeded",
    )
    Message.create(
        conversation=run.conversation_id,
        role="assistant",
        source="voice_backend",
        content="Your meeting is booked.",
        status="final",
        client_request_id=f"assistant:{run.id}",
    )
    emit(run.id, "message.delta", {"text": "Your meeting is booked."})
    monkeypatch.setattr(voice, "delegate", lambda *args: (run.id, "task"))
    manager = VoiceManager(Settings())
    connection = VoiceConnection(manager, call.id)
    manager.connections[call.id] = connection
    connection.send = AsyncMock()
    manager.stop = AsyncMock()
    try:
        await connection.delegation(delegation())
        assert connection.end_request
        manager.stop.assert_not_called()
        assert not await manager.playback_finished(call.id, "old-close-request")
        assert await manager.playback_finished(call.id, connection.end_request)
        manager.stop.assert_awaited_once_with(call.id)
        assert (
            json.loads(VoiceEvent.get(VoiceEvent.kind == "voice.end_requested").payload)["reason"]
            == "booking_complete"
        )
    finally:
        connection.end_task.cancel()
        await asyncio.gather(connection.end_task, return_exceptions=True)
        manager.connections.clear()
        await manager.http.aclose()


@pytest.mark.asyncio
async def test_direct_end_request_needs_no_scheduling_run_or_booking(account):
    user, _ = account
    call = session(user)
    voice.fragment(call.id, transcript("end", "Please end call", 100, 500))
    manager = VoiceManager(Settings())
    connection = VoiceConnection(manager, call.id)
    connection.send = AsyncMock()
    try:
        await connection.delegation(delegation())
        assert connection.end_request
        assert not Run.select().exists()
        assert not Action.select().exists()
        connection.send.assert_any_await("session.input_audio.mute")
    finally:
        connection.end_task.cancel()
        await asyncio.gather(connection.end_task, return_exceptions=True)
        await manager.http.aclose()


@pytest.mark.asyncio
async def test_recorded_hangup_phrase_closes_the_sideband_without_model_delegation(
    account, monkeypatch
):
    user, _ = account
    call = session(user)
    farewell = asyncio.Event()
    sent = []

    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

        async def recv(self):
            return await self.queue.get()

        async def send(self, raw):
            event = json.loads(raw)
            sent.append(event["type"])
            if event["type"] == "session.commentary.append":
                farewell.set()
            elif event["type"] == "session.close":
                await self.queue.put(json.dumps({"type": "session.closed"}))

    socket = Socket()
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *args, **kwargs: socket)
    manager = VoiceManager(Settings(voice_api_key="offline-key"))
    connection = VoiceConnection(manager, call.id)
    manager.connections[call.id] = connection
    connection.task = asyncio.create_task(connection.run("offline-session"))
    try:
        await asyncio.wait_for(connection.ready.wait(), 2)
        await socket.queue.put(
            json.dumps(transcript("hangup", "Okay, nice. End call", 159200, 160800))
        )
        await asyncio.wait_for(farewell.wait(), 2)
        assert connection.end_request
        assert not Run.select().exists()
        event = VoiceEvent.get(
            VoiceEvent.session == call.id, VoiceEvent.kind == "voice.end_requested"
        )
        assert json.loads(event.payload)["reason"] == "user_requested"
        assert await asyncio.wait_for(manager.playback_finished(call.id, connection.end_request), 2)
        assert connection.closed.is_set()
        assert VoiceSession.get_by_id(call.id).status == "closed"
        assert "session.input_audio.mute" in sent
        assert "session.close" in sent
        assert call.id not in manager.connections
        assert not Action.select().exists()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_mute", [True, False])
async def test_failed_farewell_send_still_publishes_hangup_and_arms_timeout(account, fail_mute):
    user, _ = account
    call = session(user)
    manager = VoiceManager(Settings())
    connection = VoiceConnection(manager, call.id)
    connection.send = AsyncMock(
        side_effect=[RuntimeError("unavailable")]
        if fail_mute
        else [None, RuntimeError("unavailable")]
    )
    try:
        await connection.request_end("user_requested")
        event = VoiceEvent.get(
            VoiceEvent.session == call.id, VoiceEvent.kind == "voice.end_requested"
        )
        assert json.loads(event.payload)["request_id"] == connection.end_request
        assert connection.end_task and not connection.end_task.done()
        assert VoiceEvent.select().where(VoiceEvent.kind == "voice.notice").exists()
    finally:
        connection.end_task.cancel()
        await asyncio.gather(connection.end_task, return_exceptions=True)
        await manager.http.aclose()


@pytest.mark.asyncio
async def test_explicit_hangup_has_a_short_server_deadline_without_browser_ack(
    account, monkeypatch
):
    user, _ = account
    call = session(user)
    manager = VoiceManager(Settings())
    connection = VoiceConnection(manager, call.id)
    connection.end_request, connection.end_reason = "hangup", "user_requested"
    manager.stop = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("app.providers.voice.asyncio.sleep", sleep)
    try:
        await connection.end_timeout("hangup")
        sleep.assert_awaited_once_with(15)
        manager.stop.assert_awaited_once_with(call.id)
    finally:
        await manager.http.aclose()
