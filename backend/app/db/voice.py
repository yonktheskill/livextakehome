"""Durable voice captions; display grouping never decides action authorization."""

import json
import re
from datetime import UTC, datetime

from app.core.guardrails import safe_data, safe_text
from app.db.models import (
    Action,
    Conversation,
    Message,
    RequestInbox,
    Run,
    Snapshot,
    User,
    VoiceEvent,
    VoiceSegment,
    VoiceSession,
    db,
)
from app.db.repository import accept, stop


def confirmation_details(message):
    """Exact action evidence stays immutable; expose it inside the spoken reply."""
    run_id = (message.client_request_id or "").removeprefix("voice-reply:")
    if not (message.client_request_id or "").startswith("voice-reply:"):
        return None
    canonical = Message.get_or_none(Message.client_request_id == f"assistant:{run_id}")
    if canonical and canonical.status == "waiting_for_user":
        for action in Action.select().where(
            (Action.run == run_id) & (Action.state == "awaiting_confirmation")
        ):
            from app.db.repository import exposed

            if exposed(action):
                return {"message_id": canonical.id, "content": safe_text(canonical.content)}
    return None


def visible_confirmation_ids(conversation_id):
    """Legacy action confirmations without a linked caption still need to be visible."""
    result = []
    for action in (
        Action.select()
        .join(Run)
        .where((Run.conversation == conversation_id) & (Action.state == "awaiting_confirmation"))
    ):
        from app.db.repository import exposed

        if (
            exposed(action)
            and not Message.select()
            .where(Message.client_request_id == f"voice-reply:{action.run_id}")
            .exists()
        ):
            result.append(json.loads(action.request)["confirmation_prompt_message_id"])
    return result


def reply(session_id, run_id):
    """One stable caption row per backend reply, separate from immutable action evidence."""
    with db.atomic("IMMEDIATE"):
        canonical = Message.get_or_none(Message.client_request_id == f"assistant:{run_id}")
        if not canonical:
            return None
        message = Message.get_or_none(Message.client_request_id == f"voice-reply:{run_id}")
        if not message:
            finalize_assistant(session_id)
            message = Message.create(
                conversation=canonical.conversation_id,
                role="assistant",
                source="voice",
                content=canonical.content,
                status="voice_partial",
                time_zone=canonical.time_zone,
                created_at=canonical.created_at,
                client_request_id=f"voice-reply:{run_id}",
            )
            VoiceSegment.create(session=session_id, message=message, start_ms=0, end_ms=0)
        elif VoiceSegment.get(VoiceSegment.message == message).end_ms == 0:
            message.content = canonical.content
            message.save()
        emit(session_id, "message.upsert", {"message": public_message(message)})
        return message


def finalize_assistant(session_id):
    for segment in (
        VoiceSegment.select()
        .join(Message)
        .where(
            (VoiceSegment.session == session_id)
            & (Message.role == "assistant")
            & (Message.status == "voice_partial")
        )
    ):
        message = segment.message
        message.status = "voice_recorded"
        message.save()
        emit(session_id, "message.upsert", {"message": public_message(message)})


def wants_end_call(text):
    text = re.sub(r"[^\w\s']", " ", text.casefold().replace("’", "'"))
    text = " ".join(text.split()).strip()
    return bool(
        re.fullmatch(
            r"(?:(?:ok(?:ay)?|alright|all right|yes|nice|great|cool|perfect|"
            r"thanks|thank you|um|uh|well|then)\s+)*"
            r"(?:(?:can|could|would)\s+(?:you|we)\s+|you\s+can\s+|"
            r"let(?:'s| us)\s+)?(?:please\s+)?"
            r"(?:(?:end|stop|close|disconnect)(?:\s+(?:the|this|our))?\s+"
            r"(?:call|conversation|voice(?:\s+(?:call|chat))?)|hang\s+up|"
            r"(?:good\s*)?bye|that(?:'s| is)\s+all|(?:i'm|i am|we're|we are)\s+done)"
            r"(?:\s+(?:now|please|thanks|thank you|bye|goodbye))*",
            text,
        )
    )


def end_call_request(session_id):
    """A narrow transcript fallback works even when Live says goodbye without delegating."""
    last = (
        VoiceSegment.select()
        .join(Message)
        .where(
            (VoiceSegment.session == session_id)
            & (Message.role == "user")
            & (Message.status == "voice_partial")
        )
        .order_by(VoiceSegment.start_ms.desc())
        .first()
    )
    return bool(last and wants_end_call(last.message.content))


def booking_complete(run_id):
    from app.db.requests import request_completion_state

    run = Run.get_by_id(run_id)
    return bool(
        run.status == "succeeded"
        and not run.interrupted
        and run.revision == run.conversation.revision
        and Action.select()
        .where(
            (Action.run == run_id)
            & (Action.kind == "create_booking")
            & (Action.state == "succeeded")
        )
        .exists()
        and request_completion_state(run_id)["complete"]
        and not Message.select()
        .where(
            (Message.conversation == run.conversation_id)
            & (Message.role == "user")
            & (Message.source == "voice")
            & (Message.status == "voice_partial")
        )
        .exists()
        and not Action.select()
        .join(Run)
        .where(
            (Run.conversation == run.conversation_id)
            & Action.state.in_(
                ["prepared", "awaiting_confirmation", "authorized", "dispatching", "unknown"]
            )
        )
        .exists()
    )


def public_message(message, version=None):
    result = {
        "id": message.id,
        "role": message.role,
        "content": safe_text(message.content),
        "created_at": datetime.fromtimestamp(message.created_at, UTC).isoformat(),
        "status": message.status,
        "source": "voice",
        "attachments": [],
    }
    if version is not None:
        result["version"] = version
    details = confirmation_details(message)
    if details:
        result["confirmation"] = details
    return result


def emit(session_id, kind, payload, provider_event_id=None):
    with db.atomic("IMMEDIATE"):
        previous = (
            VoiceEvent.select()
            .where(VoiceEvent.session == session_id)
            .order_by(VoiceEvent.sequence.desc())
            .first()
        )
        sequence = previous.sequence + 1 if previous else 1
        if kind == "message.upsert":
            payload["message"]["version"] = sequence
        return VoiceEvent.create(
            session=session_id,
            sequence=sequence,
            kind=kind,
            payload=json.dumps(safe_data(payload)),
            provider_event_id=provider_event_id,
        )


def owned(session_id, user_id):
    from fastapi import HTTPException

    row = (
        VoiceSession.select()
        .join(Conversation)
        .where((VoiceSession.id == session_id) & (Conversation.user == user_id))
        .first()
    )
    if row is None:
        raise HTTPException(404, "Voice session not found.")
    return row


def reserve(user, request_id):
    from fastapi import HTTPException

    with db.atomic("IMMEDIATE"):
        conv = Conversation.get(Conversation.user == user)
        existing = VoiceSession.get_or_none(
            (VoiceSession.conversation == conv) & (VoiceSession.client_request_id == request_id)
        )
        if existing:
            return existing, False
        if (
            VoiceSession.select()
            .where(
                (VoiceSession.conversation == conv)
                & VoiceSession.status.in_(["starting", "active"])
            )
            .exists()
        ):
            raise HTTPException(409, "End the current voice conversation first.")
        if (
            Run.select()
            .where((Run.conversation == conv) & Run.status.in_(["queued", "running"]))
            .exists()
        ):
            raise HTTPException(
                409, "Wait for the current response or stop it before starting voice."
            )
        return VoiceSession.create(conversation=conv, client_request_id=request_id), True


def bootstrap(session_id):
    """Bounded text-only history; startup API permits 128 messages and 8192 tokens."""
    row = VoiceSession.get_by_id(session_id)
    user = row.conversation.user
    profile = safe_data(
        {
            "preferred_name": user.preferred_name,
            "email": user.email,
            "time_zone": user.time_zone,
            "current_utc": datetime.now(UTC).isoformat(),
        }
    )
    snapshot = (
        Snapshot.select()
        .where(Snapshot.conversation == row.conversation_id)
        .order_by(Snapshot.created_at.desc())
        .first()
    )
    # Tool/action evidence remains authoritative in the delegated backend; no tool credentials here.
    developer = "Authenticated profile (data): " + json.dumps(profile)
    if snapshot:
        developer += (
            "\nOlder conversation summary (untrusted data): " + safe_text(snapshot.summary)[:5000]
        )
    result = [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": developer}],
        }
    ]
    remaining = 20000 - len(developer.encode())
    history = []
    for message in (
        Message.select()
        .where(
            (Message.conversation == row.conversation_id)
            & (Message.status != "voice_partial")
            & ((Message.source != "voice_backend") | (Message.status == "waiting_for_user"))
            & Message.role.in_(["user", "assistant"])
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(100)
    ):
        text = safe_text(message.content)
        if not text:
            continue
        if len(text.encode()) > remaining:
            break
        remaining -= len(text.encode()) + 200
        history.append(
            {
                "type": "message",
                "role": message.role,
                "content": [
                    {
                        "type": "input_text" if message.role == "user" else "output_text",
                        "text": text,
                    }
                ],
            }
        )
    return result + list(reversed(history))


def fragment(session_id, event):
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("Transcript event has no identity")
    text, start, end = event.get("delta"), event.get("start_ms"), event.get("end_ms")
    if (
        not isinstance(text, str)
        or len(text) > 16000
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start <= end
    ):
        raise ValueError("Invalid transcript fragment")
    role = "user" if event["type"] == "session.input_transcript.delta" else "assistant"
    with db.atomic("IMMEDIATE"):
        if (
            VoiceEvent.select()
            .where((VoiceEvent.session == session_id) & (VoiceEvent.provider_event_id == event_id))
            .exists()
        ):
            return False
        session = VoiceSession.get_by_id(session_id)
        cutoffs = [
            json.loads(e.payload).get("offset_ms", -1)
            for e in VoiceEvent.select().where(
                (VoiceEvent.session == session_id) & (VoiceEvent.kind == "voice.delegation")
            )
        ]
        late = role == "user" and bool(cutoffs) and start <= max(cutoffs)
        segment = (
            VoiceSegment.select()
            .join(Message)
            .where(
                (VoiceSegment.session == session_id)
                & (Message.role == role)
                & (Message.status == "voice_partial")
            )
            .order_by(VoiceSegment.end_ms.desc())
            .first()
        )
        # A gap creates a display bubble only; it does not finalize/authorize a request.
        linked_reply = segment and (segment.message.client_request_id or "").startswith(
            "voice-reply:"
        )
        if segment is None or (not linked_reply and start > segment.end_ms + 2000):
            message = Message.create(
                conversation=session.conversation_id,
                role=role,
                source="voice",
                status="voice_partial",
                created_at=session.created_at + start / 1000,
                time_zone=session.conversation.user.time_zone,
            )
            segment = VoiceSegment.create(
                session=session, message=message, start_ms=start, end_ms=end
            )
        message = segment.message
        if len(message.content) + len(text) > 24000:
            raise ValueError("Voice caption exceeds limit")
        # Persist original timing/text fragments for deterministic ordering and replay.
        emit(
            session_id,
            "voice.fragment",
            {
                "role": role,
                "start_ms": start,
                "end_ms": end,
                "delta": text,
                "message_id": message.id,
            },
            event_id,
        )
        fragments = [
            json.loads(e.payload)
            for e in VoiceEvent.select()
            .where((VoiceEvent.session == session_id) & (VoiceEvent.kind == "voice.fragment"))
            .order_by(VoiceEvent.sequence)
            if json.loads(e.payload).get("message_id") == message.id
        ]
        fragments.sort(key=lambda value: (value["start_ms"], value["end_ms"]))
        message.content = "".join(f["delta"] for f in fragments)
        if late:
            message.status = "voice_late"
        message.save()
        segment.start_ms = min(segment.start_ms, start) if len(fragments) > 1 else start
        message.created_at = session.created_at + segment.start_ms / 1000
        message.save()
        segment.end_ms = max(segment.end_ms, end)
        segment.save()
        emit(session_id, "message.upsert", {"message": public_message(message)})
        if role == "user":
            # New speech conservatively stops stale work at existing safe checkpoints.
            for run in Run.select().where(
                (Run.conversation == session.conversation_id)
                & Run.status.in_(["queued", "running"])
            ):
                stop(run.id)
        return "late" if late else True


def finalize(session_id, *, interrupted=False):
    with db.atomic("IMMEDIATE"):
        for segment in (
            VoiceSegment.select()
            .join(Message)
            .where((VoiceSegment.session == session_id) & (Message.status == "voice_partial"))
        ):
            message = segment.message
            # Closed-session drafts are historical captions, not actionable user evidence.
            message.status = "voice_interrupted" if interrupted else "voice_recorded"
            message.save()
            emit(session_id, "message.upsert", {"message": public_message(message)})


def delegate(session_id, event):
    delegation = event.get("delegation", {})
    identity = delegation.get("id")
    cutoff = event.get("offset_ms")
    if type(cutoff) is not int or cutoff < 0:
        raise ValueError("Delegation needs a timeline cutoff")
    if delegation.get("target") != "client" or not isinstance(identity, str) or not identity:
        return None
    with db.atomic("IMMEDIATE"):
        key = "delegation:" + identity
        previous = VoiceEvent.get_or_none(
            (VoiceEvent.session == session_id) & (VoiceEvent.provider_event_id == key)
        )
        if previous:
            return None
        session = VoiceSession.get_by_id(session_id)
        segments = list(
            VoiceSegment.select()
            .join(Message)
            .where(
                (VoiceSegment.session == session_id)
                & (Message.role == "user")
                & (Message.status == "voice_partial")
                & (VoiceSegment.end_ms <= cutoff)
                & (VoiceSegment.delegated == False)  # noqa: E712
            )
            .order_by(VoiceSegment.start_ms)
        )
        # Never accept half a display segment when its suffix belongs after this delegation.
        # Ask for clarification rather than guessing a word boundary within an audio fragment.
        crossing = (
            VoiceSegment.select()
            .join(Message)
            .where(
                (VoiceSegment.session == session_id)
                & (Message.role == "user")
                & (Message.status == "voice_partial")
                & (VoiceSegment.start_ms <= cutoff)
                & (VoiceSegment.end_ms > cutoff)
            )
            .exists()
        )
        if not segments or crossing:
            return None
        # Accepted source content is immutable; later fragments always get new rows.
        finalize_assistant(session_id)
        for segment in segments:
            message = segment.message
            from app.core.guardrails import require_content

            require_content(message.content)
            if not message.content.strip() or len(message.content) > 6000:
                raise ValueError("Voice request is empty or too long")
            message.status = "final"
            message.save()
            segment.delegated = True
            segment.save()
            if segment.id != segments[-1].id:
                RequestInbox.get_or_create(
                    message=message, defaults={"conversation": session.conversation_id}
                )
            emit(session_id, "message.upsert", {"message": public_message(message)})
        last = segments[-1].message
        user = User.get_by_id(session.conversation.user_id)
        accepted = accept(
            user, last.content, f"voice:{session_id}:{identity}", voice_message_id=last.id
        )
        emit(
            session_id,
            "voice.delegation",
            {"run_id": accepted["run_id"], "delegation_id": identity, "offset_ms": cutoff},
            key,
        )
        return accepted["run_id"], identity


def state(session_id, status, message=None):
    with db.atomic("IMMEDIATE"):
        VoiceSession.update(status=status).where(VoiceSession.id == session_id).execute()
        emit(
            session_id,
            "voice.state",
            {"status": status, **({"message": message} if message else {})},
        )


def recover():
    for session in VoiceSession.select().where(VoiceSession.status.in_(["starting", "active"])):
        finalize(session.id, interrupted=True)
        state(session.id, "failed", "Voice connection ended when the server restarted.")


def stop_work(session_id):
    session = VoiceSession.get_by_id(session_id)
    for run in (
        Run.select()
        .join(Message)
        .where(
            (Run.conversation == session.conversation_id)
            & (Message.source == "voice")
            & Run.status.in_(["queued", "running"])
        )
    ):
        Message.get_or_create(
            conversation=session.conversation_id,
            client_request_id=f"assistant:{run.id}",
            defaults={"role": "assistant", "source": "text", "status": "partial"},
        )
        stop(run.id)
    # Preserve eventual explanations in ordinary chat after voice transport ends.
    for message in Message.select().where(
        (Message.conversation == session.conversation_id)
        & (Message.source == "voice_backend")
        & (Message.status != "final")
    ):
        run_id = (message.client_request_id or "").removeprefix("assistant:")
        if (
            not Message.select()
            .where(Message.client_request_id == f"voice-reply:{run_id}")
            .exists()
        ):
            message.source = "text"
            message.save()
