"""Persisted observations are untrusted source data, never canonical user text."""

import json

from app.db.models import Attachment, Extraction, Message, RequestInbox, Run


def message_attachments(message_id):
    from app.db.attachments import metadata

    return [
        metadata(a)
        for a in Attachment.select(
            Attachment.id,
            Attachment.mime_type,
            Attachment.width,
            Attachment.height,
            Attachment.size_bytes,
        )
        .where(Attachment.message == message_id)
        .order_by(Attachment.position)
    ]


def observations(message_id):
    row = Extraction.get_or_none(Extraction.message == message_id)
    return json.loads(row.observations) if row else None


def source_images(message_id):
    attachments = message_attachments(message_id)
    if not attachments:
        return {}
    return {
        "attachments": attachments,
        "image_observations_untrusted": observations(message_id),
    }


def pending_images(run_id, version):
    run = Run.get_by_id(run_id)
    messages = (
        Message.select()
        .join(RequestInbox)
        .where(
            (Message.conversation == run.conversation_id)
            & (RequestInbox.state == "pending")
            & (Message.created_at <= run.message.created_at)
        )
    )
    result = []
    for message in messages:
        cached = Extraction.get_or_none(Extraction.message == message.id)
        images = list(
            Attachment.select()
            .where(Attachment.message == message.id)
            .order_by(Attachment.position)
        )
        if images and (not cached or cached.version != version):
            result.append(
                (
                    message.id,
                    message.content,
                    [{"id": a.id, "mime_type": a.mime_type, "data": bytes(a.data)} for a in images],
                )
            )
    return result


def cache(message_id, version, value):
    Extraction.insert(
        message=message_id, version=version, observations=json.dumps(value)
    ).on_conflict(
        conflict_target=[Extraction.message],
        update={Extraction.version: version, Extraction.observations: json.dumps(value)},
    ).execute()


def ambiguous_fields(message_id):
    value = observations(message_id) or {}
    return sorted(
        {
            key
            for candidate in value.get("candidates", [])
            for key, field in candidate.get("fields", {}).items()
            if field.get("status") == "ambiguous"
            and key in {"start", "attendee", "duration_minutes", "event_type", "time_zone"}
        }
    )
