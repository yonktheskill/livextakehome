import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.agent.context import assemble, retrieve
from app.core.config import Settings
from app.db import voice
from app.db.models import (
    Conversation,
    Message,
    RequestInbox,
    Run,
    VoiceEvent,
    VoiceSegment,
    VoiceSession,
)
from app.db.repository import accept
from app.db.requests import (
    RequestConflict,
    assert_requests_ready,
    request_context,
    resolve_requests,
)
from app.providers.voice import VoiceManager, chunks
from app.tools.executor import Executor


def session(user):
    return voice.reserve(user, str(uuid4()))[0]


def transcript(identity, text, start, end, role="user"):
    return {
        "type": "session.input_transcript.delta"
        if role == "user"
        else "session.output_transcript.delta",
        "event_id": identity,
        "delta": text,
        "start_ms": start,
        "end_ms": end,
    }


def delegation(identity="task", cutoff=1000):
    return {"delegation": {"id": identity, "target": "client"}, "offset_ms": cutoff}


def test_captions_dedupe_order_and_no_agent_run(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("b", " world", 200, 300))
    voice.fragment(row.id, transcript("a", "Hello", 100, 200))
    assert not voice.fragment(row.id, transcript("a", "Hello", 100, 200))
    assert not Run.select().exists()
    assert not RequestInbox.select().exists()
    assert (
        "".join(m.content for m in Message.select().order_by(Message.created_at)) == "Hello world"
    )
    assert VoiceEvent.select().where(VoiceEvent.kind == "voice.fragment").count() == 2


def test_delegation_accepts_once_and_freezes_sources(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Book a meeting", 100, 300))
    result = voice.delegate(row.id, delegation())
    assert result
    run = Run.get_by_id(result[0])
    source_id = run.message_id
    assert run.message.content == "Book a meeting"
    assert run.message.status == "final"
    assert voice.delegate(row.id, delegation()) is None
    voice.fragment(row.id, transcript("b", "Actually tomorrow", 1100, 1500))
    assert Message.get_by_id(source_id).content == "Book a meeting"
    assert Run.get_by_id(run.id).interrupted
    assert Run.select().count() == 1


@pytest.mark.parametrize("later_typed_request", [False, True])
def test_all_delegated_voice_segments_are_visible_and_resolved_in_order(
    account, later_typed_request
):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Oh yeah, I cannot", 100, 500))
    # The pause creates two display bubbles, but only the last gets its own Run.
    voice.fragment(row.id, transcript("b", "Move Alice's meeting to 12:45", 4000, 4500))
    run_id, _ = voice.delegate(row.id, delegation(cutoff=5000))
    if later_typed_request:
        run_id = accept(user, "Please make that change", str(uuid4()))["run_id"]
    run = Run.get_by_id(run_id)
    inbox = list(RequestInbox.select().order_by(RequestInbox.id))
    assert not Run.select().where(Run.message == inbox[0].message_id).exists()
    # Approximate audio timestamps must not determine inbox acceptance order.
    Message.update(created_at=run.message.created_at + 60).where(
        Message.id == inbox[0].message_id
    ).execute()
    assert [item["message_id"] for item in request_context(run_id, limit=1)["inbox"]] == [
        inbox[0].message_id
    ]
    assert request_context(run_id, limit=1)["inbox_truncated"]
    resolution = [{"relation": "dismiss", "reason": "No new booking task in this segment"}]
    with pytest.raises(RequestConflict, match="older"):
        resolve_requests(run_id, run.message_id, resolution)
    with pytest.raises(RequestConflict, match="every pending"):
        assert_requests_ready(run_id)
    for pending in inbox:
        ctx = request_context(run_id, limit=1)
        assert ctx["inbox"][0]["message_id"] == pending.message_id
        resolve_requests(run_id, pending.message_id, resolution)
    assert request_context(run_id)["inbox"] == []
    assert_requests_ready(run_id)


def test_voice_inbox_context_excludes_segments_accepted_after_its_run(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "First segment", 100, 500))
    voice.fragment(row.id, transcript("b", "Second segment", 4000, 4500))
    first_run, _ = voice.delegate(row.id, delegation(cutoff=5000))
    first_ids = [i.message_id for i in RequestInbox.select().order_by(RequestInbox.id)]
    voice.fragment(row.id, transcript("c", "Later segment", 6000, 6500))
    voice.fragment(row.id, transcript("d", "Last segment", 10000, 10500))
    second_run, _ = voice.delegate(row.id, delegation("second", cutoff=11000))
    all_ids = [i.message_id for i in RequestInbox.select().order_by(RequestInbox.id)]
    assert len(all_ids) == 4
    assert [i["message_id"] for i in request_context(first_run)["inbox"]] == first_ids
    assert [i["message_id"] for i in request_context(second_run)["inbox"]] == all_ids


def test_cutoff_does_not_accept_newer_or_crossing_speech(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Book today", 100, 300))
    voice.fragment(row.id, transcript("b", " no tomorrow", 400, 600))
    assert voice.delegate(row.id, delegation(cutoff=350)) is None
    assert not Run.select().exists()
    result = voice.delegate(row.id, delegation(cutoff=700))
    assert Run.get_by_id(result[0]).message.content == "Book today no tomorrow"


def test_late_transcript_never_mutates_accepted_evidence(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Book today", 100, 300))
    result = voice.delegate(row.id, delegation(cutoff=500))
    assert voice.fragment(row.id, transcript("late", " no", 300, 450)) == "late"
    run = Run.get_by_id(result[0])
    assert run.message.content == "Book today"
    assert run.interrupted
    assert voice.delegate(row.id, delegation("another", 600)) is None


def test_voice_drafts_excluded_context_and_authorization(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Private unfinished request", 0, 10))
    draft = Message.select().get()
    accepted = accept(user, "What is next?", str(uuid4()))
    run = Run.get_by_id(accepted["run_id"])
    # Put the fixture's draft before the typed request deterministically.
    draft.created_at = run.message.created_at - 1
    draft.save()
    assert "Private unfinished request" not in json.dumps(assemble(run.id))
    assert "Private unfinished request" not in json.dumps(retrieve(run.id, "", role="user"))
    assert Executor.user_source(run, draft.id) is None
    voice.finalize(row.id, interrupted=True)
    assert Message.get_by_id(draft.id).status == "voice_interrupted"
    assert Executor.user_source(run, draft.id) is None


def test_bootstrap_includes_text_voice_and_canonical_confirmation(account):
    user, _ = account
    conv = Conversation.get(Conversation.user == user)
    Message.create(conversation=conv, role="user", content="Original request")
    Message.create(
        conversation=conv,
        role="assistant",
        content="Spoken answer",
        source="voice",
        status="voice_recorded",
    )
    Message.create(
        conversation=conv,
        role="assistant",
        content="Confirm cancellation?",
        source="voice_backend",
        status="waiting_for_user",
    )
    Message.create(
        conversation=conv, role="user", content="Unfinished", source="voice", status="voice_partial"
    )
    row = session(user)
    context = json.dumps(voice.bootstrap(row.id))
    assert "Original request" in context and "Spoken answer" in context
    assert "Confirm cancellation?" in context and "Unfinished" not in context


def test_session_ownership_and_reservation(account):
    user, _ = account
    row, fresh = voice.reserve(user, "request")
    assert fresh
    same, fresh = voice.reserve(user, "request")
    assert same.id == row.id and not fresh
    with pytest.raises(HTTPException) as error:
        voice.reserve(user, "different")
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        voice.owned(row.id, user.id + 1)
    assert error.value.status_code == 404


def test_stop_work_uses_existing_safe_stop_and_reveals_results(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Book tomorrow", 0, 100))
    run_id, _ = voice.delegate(row.id, delegation())
    voice.stop_work(row.id)
    assert Run.get_by_id(run_id).interrupted
    assert Message.get(Message.client_request_id == f"assistant:{run_id}").source == "text"


def test_recover_finalizes_session_and_preserves_logs(account):
    user, _ = account
    row = session(user)
    voice.fragment(row.id, transcript("a", "Hello", 0, 100))
    voice.recover()
    assert VoiceSession.get_by_id(row.id).status == "failed"
    assert Message.get().content == "Hello"
    assert Message.get().status == "voice_interrupted"
    assert VoiceSegment.select().count() == 1


@pytest.mark.asyncio
async def test_voice_disabled_independent_chat_configuration():
    manager = VoiceManager(Settings(voice_enabled=False, llm_provider="codex"))
    assert not manager.enabled
    await manager.aclose()
    manager = VoiceManager(Settings(voice_enabled=True, voice_api_key="test", llm_provider="codex"))
    assert manager.enabled
    await manager.aclose()


def test_backend_update_chunks_keep_unicode_and_bound_size():
    text = "Very long ✅ result. " * 500
    parts = list(chunks(text))
    assert "".join(parts) == text
    assert all(len(part.encode()) <= 450 for part in parts)


@pytest.mark.asyncio
async def test_rest_webrtc_creation_sideband_mute_and_graceful_close(account, monkeypatch):
    import asyncio

    import httpx

    from app.providers.voice import VoiceConnection

    user, _ = account
    manager = VoiceManager(Settings(voice_enabled=True, voice_api_key="private-test-key"))
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
            value = json.loads(raw)
            sent.append(value)
            if value["type"] == "session.input_audio.mute":
                await self.queue.put(
                    json.dumps(
                        {
                            "type": "session.input_audio.muted",
                            "client_event_id": value["event_id"],
                        }
                    )
                )
            if value["type"] == "session.close":
                # Final transcript may precede the session.closed marker.
                await self.queue.put(
                    json.dumps(transcript("final", "Final spoken reply", 0, 100, "assistant"))
                )
                await self.queue.put(json.dumps({"type": "session.closed"}))

    socket = Socket()
    seen = []

    def connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/live/sessions/live_opaque/attach"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer private-test-key"
        return socket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            201, json={"session": {"id": "live_opaque"}, "transport": {"sdp": "answer"}}
        )

    await manager.http.aclose()
    manager.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    result = await manager.create(user, str(uuid4()), "offer")
    assert result["sdp"] == "answer"
    assert seen[0]["session"]["delegation"] == {"type": "client"}
    assert seen[0]["transport"] == {"type": "webrtc", "sdp": "offer"}
    assert "private-test-key" not in json.dumps(seen)
    connection: VoiceConnection = manager.connections[result["id"]]
    await connection.command("session.input_audio.mute", "session.input_audio.muted")
    await manager.stop(result["id"])
    assert any(value["type"] == "session.close" for value in sent)
    assert VoiceSession.get_by_id(result["id"]).status == "closed"
    assert Message.get().content == "Final spoken reply"
    assert not manager.connections
    await manager.aclose()


def test_v6_upgrade_adds_source_without_dropping_messages(account):
    from playhouse.migrate import SqliteMigrator, migrate

    from app.db.database import initialize
    from app.db.models import SchemaVersion, db

    user, _ = account
    conv = Conversation.get(Conversation.user == user)
    original = Message.create(conversation=conv, role="user", content="Keep original")
    migrate(SqliteMigrator(db).drop_column("message", "source"))
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=6)
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        assert Message.get_by_id(original.id).content == "Keep original"
        assert Message.get_by_id(original.id).source == "text"
