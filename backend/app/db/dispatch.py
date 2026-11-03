"""Canonical immutable dispatch evidence and atomic user-visible settlement."""

import json

from app.core.security import digest
from app.db.models import Action, Confirmation, Connection, Message, Run, db
from app.db.repository import checkpoint, emit


class StalePreparation(Exception):
    pass


def prepare_action(run, conn, name, encoded, fingerprint, confirmation_required, request=None):
    with db.atomic("IMMEDIATE"):
        live = Run.get_by_id(run.id)
        connection = Connection.get_or_none(Connection.user == live.conversation.user_id)
        if (
            not checkpoint(live.id)
            or not connection
            or connection.generation != conn.generation
            or not connection.ciphertext
        ):
            raise StalePreparation()
        existing = Action.get_or_none(
            (Action.run == live.id)
            & (Action.kind == name)
            & (Action.inputs == encoded)
            & (Action.state != "superseded")
        )
        if existing:
            if existing.state in {"dispatching", "unknown", "succeeded", "failed"}:
                return existing.id, False
            if existing.fingerprint == fingerprint:
                return existing.id, existing.state == "awaiting_confirmation"
            existing.state = "superseded"
            existing.logical_key = None
            existing.save()
        action = Action.create(
            run=live.id,
            revision=live.revision,
            generation=connection.generation,
            kind=name,
            inputs=encoded,
            logical_key=digest(f"{live.id}:{name}:{encoded}"),
            request=json.dumps(request or {}, sort_keys=True),
            fingerprint=fingerprint,
            state="awaiting_confirmation" if confirmation_required else "prepared",
        )
        if confirmation_required:
            import time

            Confirmation.create(
                conversation=live.conversation_id, action=action, expires=time.time() + 600
            )
        return action.id, confirmation_required


def receipt(action):
    outcome = json.loads(action.result)
    return {
        "action_id": action.id,
        "kind": action.kind,
        "summary": "Cal.com returned an action result; inspect its status.",
        "details": {
            **outcome.get("validated", {}),
            **{
                k: v
                for k, v in outcome.get("response", {}).items()
                if k in {"title", "status", "start", "end", "duration_minutes"}
            },
            "booking_uid": outcome.get("response", {}).get("uid"),
        },
    }


def settle(action_id, state, result):
    with db.atomic("IMMEDIATE"):
        action = Action.get_by_id(action_id)
        if action.state != "dispatching":
            return False  # Never overwrite terminal evidence or resurrect a stale lease.
        action.state = state
        action.result = json.dumps(result)
        action.save()
        if state == "succeeded":
            data = receipt(action)
            emit(action.run_id, "action.result", data)
            response = result.get("response", {})
            fields = {
                k: response[k]
                for k in ("uid", "id", "title", "start", "end", "status")
                if k in response
            }
            Message.create(
                conversation=action.run.conversation_id,
                role="assistant",
                client_request_id=f"action:{action.id}",
                content=(
                    f"Action receipt ({action.kind}):\n\n```json\n"
                    f"{json.dumps(fields, indent=2)}\n```"
                ),
                status="final",
            )
        return True
