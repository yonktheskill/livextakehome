import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import current
from app.db import voice as store
from app.db.database import query
from app.db.models import VoiceEvent, VoiceSession

router = APIRouter(prefix="/api/voice")


class Offer(BaseModel):
    sdp: str = Field(min_length=1, max_length=64000)
    client_request_id: UUID


class Mute(BaseModel):
    muted: bool


class PlaybackFinished(BaseModel):
    request_id: UUID


@router.post("/sessions/{session_id}/playback-finished")
async def playback_finished(session_id: str, data: PlaybackFinished, request: Request):
    user = await current(request)
    await query(store.owned, session_id, user.id)
    closed = await request.app.state.voice.playback_finished(session_id, str(data.request_id))
    return {"closed": closed}


@router.get("/config")
async def config(request: Request):
    await current(request)
    return {"enabled": request.app.state.voice.enabled}


@router.post("/sessions")
async def create(data: Offer, request: Request):
    user = await current(request)
    manager = request.app.state.voice
    if not manager.enabled:
        raise HTTPException(503, "Voice is not configured.")
    if not data.sdp.strip():
        raise HTTPException(422, "An SDP offer is required.")
    return await manager.create(user, str(data.client_request_id), data.sdp)


@router.post("/sessions/{session_id}/stop")
async def stop(session_id: str, request: Request):
    user = await current(request)
    await query(store.owned, session_id, user.id)
    await request.app.state.voice.stop(session_id)
    return {"status": "closed"}


@router.post("/sessions/{session_id}/mute")
async def mute(session_id: str, data: Mute, request: Request):
    user = await current(request)
    await query(store.owned, session_id, user.id)
    connection = request.app.state.voice.connections.get(session_id)
    if connection is None or connection.closed.is_set():
        raise HTTPException(409, "Voice session has ended.")
    await connection.send(
        "session.input_audio.mute" if data.muted else "session.input_audio.unmute"
    )
    return {"muted": data.muted}


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, request: Request, after: int = 0):
    user = await current(request)
    await query(store.owned, session_id, user.id)
    try:
        after = max(0, after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "Invalid event cursor.") from None

    def batch(cursor):
        rows = list(
            VoiceEvent.select()
            .where((VoiceEvent.session == session_id) & (VoiceEvent.sequence > cursor))
            .order_by(VoiceEvent.sequence)
            .limit(100)
        )
        return [(row.sequence, row.kind, row.payload) for row in rows], VoiceSession.get_by_id(
            session_id
        ).status

    async def stream():
        cursor = after
        heartbeat = 0
        while not await request.is_disconnected():
            rows, status = await query(batch, cursor)
            for sequence, kind, payload in rows:
                cursor = sequence
                if kind in {
                    "message.upsert",
                    "voice.state",
                    "action.result",
                    "voice.notice",
                    "voice.end_requested",
                    "voice.end_cancelled",
                }:
                    # Replay full message replacements, never concat duplicate fragments.
                    data = json.loads(payload)
                    yield f"id: {sequence}\nevent: {kind}\ndata: {json.dumps(data)}\n\n"
            if status in {"closed", "failed"} and len(rows) < 100:
                return
            if heartbeat % 40 == 0:
                yield ": keepalive\n\n"
            heartbeat += 1
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
