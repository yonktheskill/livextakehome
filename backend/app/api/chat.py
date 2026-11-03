import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import current
from app.core.guardrails import SAFE_OUTPUT, ContentRejected, reason, safe_data, safe_text
from app.db.database import query
from app.db.dispatch import receipt
from app.db.models import Action, Conversation, Event, Message, Run, Trace
from app.db.repository import TERMINAL, accept, stop

router = APIRouter(prefix="/api")


class Submission(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    client_request_id: UUID


def owned_run(run_id, user_id):
    run = (
        Run.select()
        .join(Conversation)
        .where((Run.id == run_id) & (Conversation.user == user_id))
        .first()
    )
    if not run:
        raise HTTPException(404, "Run not found.")
    return run


@router.get("/chat/history")
async def history(request: Request, before: str | None = None):
    user = await current(request)

    def load():
        conv = Conversation.get(Conversation.user == user.id)
        rows = Message.select().where(Message.conversation == conv)
        if before:
            boundary = Message.get_or_none((Message.id == before) & (Message.conversation == conv))
            if not boundary:
                raise HTTPException(400, "Invalid history cursor.")
            rows = rows.where(
                (Message.created_at < boundary.created_at)
                | ((Message.created_at == boundary.created_at) & (Message.id < boundary.id))
            )
        page = list(rows.order_by(Message.created_at.desc(), Message.id.desc()).limit(51))
        has_more = len(page) > 50
        page = page[:50]
        page_ids = [m.id for m in page]
        run_ids = Run.select(Run.id).where(Run.message.in_(page_ids))
        action_ids = [
            m.client_request_id[7:]
            for m in page
            if m.client_request_id and m.client_request_id.startswith("action:")
        ]
        receipts = (
            Action.select()
            .join(Run)
            .where(
                (Run.conversation == conv)
                & (Action.state == "succeeded")
                & ((Action.run.in_(run_ids)) | (Action.id.in_(action_ids)))
            )
        )
        return {
            "receipts": [receipt(a) for a in receipts],
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": safe_text(m.content),
                    "created_at": datetime.fromtimestamp(m.created_at, UTC).isoformat(),
                    "status": m.status,
                }
                for m in reversed(page)
            ],
            "next_cursor": page[-1].id if has_more else None,
        }

    return await query(load)


@router.post("/chat/messages")
async def submit(data: Submission, request: Request):
    user = await current(request)
    if not data.content.strip():
        raise HTTPException(422, "Message cannot be blank.")
    try:
        return await query(accept, user, data.content, str(data.client_request_id))
    except ContentRejected as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    user = await current(request)
    run = await query(owned_run, run_id, user.id)
    return {"id": run.id, "status": run.status}


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    user = await current(request)
    await query(owned_run, run_id, user.id)
    return await query(stop, run_id)


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = 0):
    user = await current(request)
    await query(owned_run, run_id, user.id)
    try:
        after = max(0, after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "Invalid event cursor.") from None

    def batch(cursor):
        run = Run.get_by_id(run_id)
        rows = list(
            Event.select()
            .where((Event.run == run_id) & (Event.sequence > cursor))
            .order_by(Event.sequence)
            .limit(100)
        )
        public = []
        checked_messages = {}
        for row in rows:
            payload = safe_data(json.loads(row.payload))
            if row.kind == "message.delta":
                # Legacy events may split a prohibited term across chunks. Check
                # the complete canonical message, not only this replay fragment.
                message_id = payload.get("message_id")
                if message_id not in checked_messages:
                    message = Message.get_or_none(
                        (Message.id == message_id) & (Message.conversation == run.conversation_id)
                    )
                    checked_messages[message_id] = not message or bool(reason(message.content))
                if checked_messages[message_id]:
                    payload["text"] = SAFE_OUTPUT
            public.append((row.sequence, row.kind, json.dumps(payload)))
        return public, run.status

    async def stream():
        cursor = after
        heartbeat = 0
        while not await request.is_disconnected():
            rows, status = await query(batch, cursor)
            for sequence, kind, payload in rows:
                yield f"id: {sequence}\nevent: {kind}\ndata: {payload}\n\n"
                cursor = sequence
            if status in TERMINAL and len(rows) < 100:
                return
            if heartbeat % 40 == 0:
                yield ": keepalive\n\n"
            heartbeat += 1
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/debug")
async def debug(run_id: str, request: Request):
    if not request.app.state.settings.debug_api:
        raise HTTPException(404, "Not found.")
    user = await current(request)
    await query(owned_run, run_id, user.id)
    return await query(
        lambda: [
            {"kind": t.kind, "metadata": safe_data(json.loads(t.payload))}
            for t in Trace.select().where(Trace.run == run_id).limit(100)
        ]
    )
