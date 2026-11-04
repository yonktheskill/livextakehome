import json
import re
import time

from app.core.guardrails import require_content, safe_data
from app.db.models import (
    Action,
    Confirmation,
    Connection,
    Conversation,
    Event,
    Message,
    RequestInbox,
    Run,
    Trace,
    User,
    db,
)

TERMINAL = {"waiting_for_user", "succeeded", "failed", "interrupted", "needs_reconciliation"}


def approval_phrase(content):
    text = re.sub(r"[,.!]", " ", content.casefold())
    text = " ".join(text.split())
    return (
        text
        if text
        in {
            "yes",
            "confirm",
            "confirmed",
            "yes confirm",
            "confirm cancellation",
            "yes please",
            "yes go ahead",
            "go ahead",
            "please do",
            "sounds good",
            "yes do it",
            "yes please do",
            "yes please go ahead",
            "that works",
            "do it",
            "absolutely",
        }
        else None
    )


def emit(run, kind, payload):
    payload = safe_data(payload)
    with db.atomic("IMMEDIATE"):
        last = Event.select().where(Event.run == run).order_by(Event.sequence.desc()).first()
        Event.create(
            run=run,
            sequence=last.sequence + 1 if last else 1,
            kind=kind,
            payload=json.dumps(payload),
        )


def finish(run_id, status, kind, payload):
    with db.atomic("IMMEDIATE"):
        if status == "succeeded":
            from app.db.requests import request_completion_state

            completion = request_completion_state(run_id)
            if not completion["complete"]:
                status, kind = "waiting_for_user", "run.waiting"
                payload = {
                    **payload,
                    **completion,
                    "message": "Pending requests remain; interpretation, clarification "
                    "or outcome reconciliation is required.",
                }
        Run.update(status=status).where(Run.id == run_id).execute()
        if status != "waiting_for_user":
            Action.update(state="superseded").where(
                (Action.run == run_id)
                & Action.state.in_(["prepared", "authorized", "awaiting_confirmation"])
            ).execute()
        emit(run_id, kind, payload)
        Message.update(status="final" if status == "succeeded" else status).where(
            Message.client_request_id == f"assistant:{run_id}"
        ).execute()


def accept(user, content, request_id, attachment_ids=None, *, voice_message_id=None):
    attachment_ids = attachment_ids or []
    require_content(content)
    with db.atomic("IMMEDIATE"):
        conv = Conversation.get(Conversation.user == user)
        existing = Message.get_or_none(
            (Message.conversation == conv) & (Message.client_request_id == request_id)
        )
        if existing:
            run = Run.get(Run.message == existing)
            return {"run_id": run.id, "message_id": existing.id, "revision": run.revision}
        from app.db.models import Attachment

        if len(attachment_ids) > 4 or len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("Supply up to four distinct attachments.")
        uploads = [Attachment.get_or_none(Attachment.id == key) for key in attachment_ids]
        if any(
            a is None
            or a.owner_id != user.id
            or a.message_id is not None
            or a.created_at < time.time() - 86400
            for a in uploads
        ):
            raise ValueError("Attachment unavailable; upload it again.")
        if sum(a.size_bytes for a in uploads) > 8 * 1024 * 1024:
            raise ValueError("Attachments exceed eight MiB.")
        if not content.strip() and not uploads:
            raise ValueError("Message cannot be blank.")
        previous_revision = conv.revision
        conv.revision += 1
        conv.save()
        if voice_message_id:
            msg = Message.get(
                (Message.id == voice_message_id)
                & (Message.conversation == conv)
                & (Message.role == "user")
                & (Message.source == "voice")
            )
            if msg.content != content or Run.select().where(Run.message == msg).exists():
                raise ValueError("Voice message has already been accepted")
            msg.client_request_id = request_id
            msg.status = "final"
            msg.save()
        else:
            msg = Message.create(
                conversation=conv,
                role="user",
                content=content,
                time_zone=User.get_by_id(user.id).time_zone,
                client_request_id=request_id,
            )
        for position, attachment in enumerate(uploads):
            attachment.message = msg
            attachment.position = position
            attachment.save()
        RequestInbox.get_or_create(message=msg, defaults={"conversation": conv})
        pending = list(
            Confirmation.select()
            .join(Action)
            .join(Run)
            .where(
                (Confirmation.conversation == conv)
                & (Confirmation.consumed == False)  # noqa: E712
                & (Confirmation.expires > time.time())
                & (Action.state == "awaiting_confirmation")
                & ((Action.task.is_null(False)) | (Action.revision == previous_revision))
                & (Run.status == "waiting_for_user")
                & ((Action.task.is_null(False)) | (Run.revision == previous_revision))
                & (Run.interrupted == False)  # noqa: E712
            )
        )
        # Require persisted canonical exposure evidence, including legacy proposals.
        pending = [
            c
            for c in pending
            if exposed(c.action)
            and (not c.action.task_id or c.action.task_revision == c.action.task.revision)
        ]
        yes = approval_phrase(content)
        selected = pending[0] if yes and len(pending) == 1 else None
        if selected and yes == "confirm cancellation":
            if selected.action.kind != "cancel_booking":
                selected = None
        Run.update(interrupted=True).where(
            (Run.conversation == conv) & (Run.status.in_(["queued", "running"]))
        ).execute()
        Action.update(state="superseded").where(
            (Action.run.in_(Run.select(Run.id).where(Run.conversation == conv)))
            & (Action.state.in_(["prepared", "authorized", "awaiting_confirmation"]))
            & Action.task.is_null()
            & (Action.id != (selected.action_id if selected else ""))
        ).execute()
        run = Run.create(conversation=conv, message=msg, revision=conv.revision)
        if selected:
            RequestInbox.update(
                state="resolved",
                resolved_run_id=run.id,
                resolution=json.dumps({"input": [], "result": [], "approval": selected.action_id}),
            ).where(RequestInbox.message == msg.id).execute()
            selected.evidence_message = msg
            selected.save()
            Action.update(run=run, revision=conv.revision, state="authorized").where(
                Action.id == selected.action_id
            ).execute()
        return {"run_id": run.id, "message_id": msg.id, "revision": conv.revision}


def checkpoint(run_id):
    run = Run.get_by_id(run_id)
    return (
        run.status == "running"
        and not run.interrupted
        and run.conversation.revision == run.revision
    )


def stop(run_id):
    with db.atomic("IMMEDIATE"):
        run = Run.get_by_id(run_id)
        from app.db.requests import block_run_requests

        block_run_requests(run_id, "stopped")
        run.interrupted = True
        run.save()
        committed = (
            Action.select()
            .where(
                (Action.run == run) & (Action.state.in_(["dispatching", "unknown", "succeeded"]))
            )
            .exists()
        )
        Action.update(state="superseded").where(
            (Action.run == run)
            & (Action.state.in_(["prepared", "authorized", "awaiting_confirmation"]))
        ).execute()
        return {"status": run.status, "dispatch_committed": committed}


def claim(action_id):
    """Same SQLite writer gate as accept/stop/connection change; no network in txn."""
    with db.atomic("IMMEDIATE"):
        action = Action.get_by_id(action_id)
        run = action.run
        conn = Connection.get_or_none(Connection.user == run.conversation.user)
        unresolved = (
            Action.select()
            .join(Run)
            .where(
                (Run.conversation == run.conversation_id)
                & (Action.state.in_(["dispatching", "unknown"]))
                & (Action.id != action.id)
            )
            .exists()
        )
        from app.db.requests import task_claimable

        if (
            not task_claimable(action)
            or action.state not in {"prepared", "authorized"}
            or not checkpoint(run.id)
            or not conn
            or not conn.ciphertext
            or conn.generation != action.generation
            or unresolved
        ):
            return False
        confirmation = Confirmation.get_or_none(Confirmation.action == action)
        if confirmation:
            if (
                confirmation.consumed
                or not confirmation.evidence_message_id
                or confirmation.expires < time.time()
            ):
                return False
            confirmation.consumed = True
            confirmation.save()
        frozen = json.loads(action.request)
        frozen["dispatch_authorization"] = {
            "revision": run.revision,
            "connection_generation": conn.generation,
            "action_id": action.id,
            "fingerprint": action.fingerprint,
            "confirmation_message_id": confirmation.evidence_message_id if confirmation else None,
        }
        action.request = json.dumps(frozen)
        action.state = "dispatching"
        action.save()
        return True


def claim_run():
    with db.atomic("IMMEDIATE"):
        active = Run.select(Run.conversation).where(Run.status == "running")
        run = (
            Run.select()
            .where((Run.status == "queued") & Run.conversation.not_in(active))
            .order_by(Run.created_at, Run.id)
            .first()
        )
        if not run:
            return None
        run.status = "running"
        run.save()
        return run.id


def exposed(action):
    frozen = json.loads(action.request or "{}")
    prompt = frozen.get("confirmation_prompt")
    message = Message.get_or_none(Message.id == frozen.get("confirmation_prompt_message_id", ""))
    return bool(
        prompt
        and message
        and message.status == "waiting_for_user"
        and message.conversation_id == action.run.conversation_id
        and message.content == frozen.get("confirmation_message_content")
        and message.content.endswith(prompt)
    )


def wait_for_user(run_id, text):
    """Persist exact prompt and activate its proposal under one writer gate."""
    from app.tools.messages import confirmation_message

    with db.atomic("IMMEDIATE"):
        if not checkpoint(run_id):
            finish(run_id, "interrupted", "run.interrupted", {})
            return
        run = Run.get_by_id(run_id)
        proposals = list(
            Action.select()
            .join(Confirmation)
            .where((Action.run == run_id) & (Action.state == "prepared"))
        )
        if len(proposals) > 1:
            raise ValueError("Ambiguous confirmation proposal")
        if proposals:
            action = proposals[0]
            frozen = json.loads(action.request)
            canonical = confirmation_message(
                action.kind, frozen.get("validation", {}), run.conversation.user.time_zone
            )
            if text != canonical:
                raise ValueError("Confirmation prompt does not match frozen action")
        message, _ = Message.get_or_create(
            conversation=run.conversation_id,
            client_request_id=f"assistant:{run_id}",
            defaults={"role": "assistant"},
        )
        delta = ("\n\n" if message.content else "") + text
        message.content += delta
        message.status = "waiting_for_user"
        message.save()
        if proposals:
            frozen["confirmation_prompt"] = text
            frozen["confirmation_prompt_message_id"] = message.id
            frozen["confirmation_message_content"] = message.content
            action.request = json.dumps(frozen)
            action.state = "awaiting_confirmation"
            action.save()
        emit(run_id, "message.delta", {"message_id": message.id, "text": delta})
        finish(run_id, "waiting_for_user", "run.waiting", {"message": text})


def recover():
    with db.atomic("IMMEDIATE"):
        from app.db.requests import block_run_requests, settle_task

        Action.update(state="unknown").where(Action.state == "dispatching").execute()
        for action in Action.select().where(Action.state == "unknown"):
            settle_task(action)
        for interrupted_run in Run.select().where(Run.status == "running"):
            block_run_requests(interrupted_run.id, "recovery_blocked")
        # Preserve only proposals whose exact prompt and waiting commit survived.
        for action in Action.select().where(Action.state == "awaiting_confirmation"):
            if action.run.status != "waiting_for_user" or not exposed(action):
                Action.update(state="superseded").where(Action.id == action.id).execute()
        Action.update(state="superseded").where(
            Action.state.in_(["prepared", "authorized"])
        ).execute()
        for run in Run.select().where(Run.status == "running"):
            unknown = (
                Action.select().where((Action.run == run) & (Action.state == "unknown")).exists()
            )
            finish(
                run.id,
                "needs_reconciliation" if unknown else "interrupted",
                "run.failed" if unknown else "run.interrupted",
                {"message": "Outcome unknown; check Cal.com before another change."}
                if unknown
                else {},
            )


def cleanup():
    from app.db.attachments import cleanup_abandoned

    cleanup_abandoned()
    return Trace.delete().where(Trace.created_at < time.time() - 30 * 86400).execute()


def mark_uncertain(run_id):
    with db.atomic("IMMEDIATE"):
        Action.update(state="unknown").where(
            (Action.run == run_id) & (Action.state == "dispatching")
        ).execute()
        from app.db.requests import settle_task

        for action in Action.select().where((Action.run == run_id) & (Action.state == "unknown")):
            settle_task(action)
        return Action.select().where((Action.run == run_id) & (Action.state == "unknown")).exists()
