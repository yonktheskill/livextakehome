import json
import time

from app.db.models import (
    Action,
    Confirmation,
    Connection,
    Conversation,
    Event,
    Message,
    Run,
    Trace,
    db,
)

TERMINAL = {"waiting_for_user", "succeeded", "failed", "interrupted", "needs_reconciliation"}


def emit(run, kind, payload):
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
        Run.update(status=status).where(Run.id == run_id).execute()
        emit(run_id, kind, payload)
        Message.update(status="final" if status == "succeeded" else status).where(
            Message.client_request_id == f"assistant:{run_id}"
        ).execute()


def accept(user, content, request_id):
    with db.atomic("IMMEDIATE"):
        conv = Conversation.get(Conversation.user == user)
        existing = Message.get_or_none(
            (Message.conversation == conv) & (Message.client_request_id == request_id)
        )
        if existing:
            run = Run.get(Run.message == existing)
            return {"run_id": run.id, "message_id": existing.id, "revision": run.revision}
        previous_revision = conv.revision
        conv.revision += 1
        conv.save()
        msg = Message.create(
            conversation=conv,
            role="user",
            content=content,
            time_zone=user.time_zone,
            client_request_id=request_id,
        )
        pending = list(
            Confirmation.select()
            .join(Action)
            .join(Run)
            .where(
                (Confirmation.conversation == conv)
                & (Confirmation.consumed == False)  # noqa: E712
                & (Confirmation.expires > time.time())
                & (Action.state == "awaiting_confirmation")
                & (Action.revision == previous_revision)
                & (Run.revision == previous_revision)
                & (Run.interrupted == False)  # noqa: E712
            )
        )
        yes = content.strip().lower() in {"yes", "confirm", "yes, confirm", "confirm cancellation"}
        selected = pending[0] if yes and len(pending) == 1 else None
        if selected and content.strip().lower() == "confirm cancellation":
            if selected.action.kind != "cancel_booking":
                selected = None
        Run.update(interrupted=True).where(
            (Run.conversation == conv) & (Run.status.in_(["queued", "running"]))
        ).execute()
        Action.update(state="superseded").where(
            (Action.run.in_(Run.select(Run.id).where(Run.conversation == conv)))
            & (Action.state.in_(["prepared", "authorized", "awaiting_confirmation"]))
            & (Action.id != (selected.action_id if selected else ""))
        ).execute()
        run = Run.create(conversation=conv, message=msg, revision=conv.revision)
        if selected:
            selected.evidence_message = msg
            selected.save()
            Action.update(run=run, revision=conv.revision, state="authorized").where(
                Action.id == selected.action_id
            ).execute()
        return {"run_id": run.id, "message_id": msg.id, "revision": conv.revision}


def checkpoint(run_id):
    run = Run.get_by_id(run_id)
    return not run.interrupted and run.conversation.revision == run.revision


def stop(run_id):
    with db.atomic("IMMEDIATE"):
        run = Run.get_by_id(run_id)
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
        if (
            action.state not in {"prepared", "authorized"}
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
        run = Run.select().where(Run.status == "queued").order_by(Run.created_at).first()
        if not run:
            return None
        run.status = "running"
        run.save()
        return run.id


def recover():
    with db.atomic("IMMEDIATE"):
        Action.update(state="unknown").where(Action.state == "dispatching").execute()
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
    return Trace.delete().where(Trace.created_at < time.time() - 30 * 86400).execute()


def mark_uncertain(run_id):
    with db.atomic("IMMEDIATE"):
        Action.update(state="unknown").where(
            (Action.run == run_id) & (Action.state == "dispatching")
        ).execute()
        return Action.select().where((Action.run == run_id) & (Action.state == "unknown")).exists()
