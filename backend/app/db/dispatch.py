"""Canonical immutable dispatch evidence and atomic user-visible settlement."""

import json
from datetime import UTC, datetime

from app.core.guardrails import safe_data
from app.core.security import digest
from app.db.models import Action, Confirmation, Connection, Message, Run, db
from app.db.repository import checkpoint, emit
from app.tools.messages import action_message


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
                return existing.id, confirmation_required and existing.state in {
                    "prepared",
                    "awaiting_confirmation",
                }
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
            state="prepared",
        )
        if confirmation_required:
            import time

            Confirmation.create(
                conversation=live.conversation_id, action=action, expires=time.time() + 600
            )
        return action.id, confirmation_required


def receipt(action, message=None):
    outcome = json.loads(action.result)
    if message is None:
        message = Message.get_or_none(
            (Message.conversation == action.run.conversation_id)
            & (Message.client_request_id == f"action:{action.id}")
        )
    return safe_data(
        {
            "action_id": action.id,
            "message_id": message.id if message else None,
            "created_at": datetime.fromtimestamp(
                message.created_at if message else action.created_at, UTC
            ).isoformat(),
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
    )


def settle(action_id, state, result):
    with db.atomic("IMMEDIATE"):
        action = Action.get_by_id(action_id)
        if action.state != "dispatching":
            return False  # Never overwrite terminal evidence or resurrect a stale lease.
        action.state = state
        action.result = json.dumps(result)
        action.save()
        if state == "succeeded":
            response = result.get("response", {})
            fields = {
                k: response[k]
                for k in ("uid", "id", "title", "start", "end", "status")
                if k in response
            }
            content = (
                f"Action receipt ({action.kind}):\n\n```json\n"
                f"{json.dumps(safe_data(fields), indent=2)}\n```"
                if action.kind == "create_booking"
                else action_message(
                    action.kind, safe_data(result), action.run.conversation.user.time_zone
                )
            )
            message = Message.create(
                conversation=action.run.conversation_id,
                role="assistant",
                client_request_id=f"action:{action.id}",
                content=content,
                status="final",
            )
            emit(action.run_id, "action.result", receipt(action, message))
            if action.kind != "create_booking":
                emit(
                    action.run_id,
                    "message.delta",
                    {"message_id": message.id, "text": message.content},
                )
        return True


def inspect_unknown(action_id):
    """Read-only frozen evidence. Digest binds resolution to this exact snapshot."""
    action = Action.get_by_id(action_id)
    evidence = {
        "action_id": action.id,
        "run_id": action.run_id,
        "state": action.state,
        "kind": action.kind,
        "inputs": json.loads(action.inputs),
        "request": json.loads(action.request or "{}"),
        "fingerprint": action.fingerprint,
        "result": json.loads(action.result),
    }
    return {"digest": digest(json.dumps(evidence, sort_keys=True)), "evidence": safe_data(evidence)}


def resolve_unknown(action_id, expected_digest, outcome, operator, note, observation):
    """Operator attestation only: never dispatch, reset a lease, or infer success."""
    from app.core.guardrails import require_content

    if outcome not in {"succeeded", "failed"} or not operator.strip() or not note.strip():
        raise ValueError("Explicit outcome, operator and verification note are required")
    require_content({"operator": operator, "note": note, "observation": observation})
    with db.atomic("IMMEDIATE"):
        action = Action.get_by_id(action_id)
        snapshot = inspect_unknown(action_id)
        if snapshot["digest"] != expected_digest:
            raise ValueError("Evidence changed; inspect again")
        if action.state != "unknown" or action.run.status in {"queued", "running"}:
            raise ValueError("Only inactive unknown actions can be reconciled")
        if not isinstance(observation, dict) or not observation:
            raise ValueError("Observed external evidence is required")
        if outcome == "succeeded":
            identity = "id" if action.kind == "create_event_type" else "uid"
            if not observation.get(identity):
                raise ValueError("Observed resource identity is required")
            frozen = json.loads(action.request or "{}")
            target = frozen.get("validation", {}).get(identity)
            if target is not None and observation[identity] != target:
                if not (
                    action.kind == "reschedule_booking"
                    and observation.get("rescheduledFromUid") == target
                ):
                    raise ValueError("Observed resource does not match frozen target or UID lineage")
            result = {"response": observation, "validated": frozen.get("validation", {})}
        else:
            result = {
                "error": "Operator verified action did not succeed",
                "observation": observation,
            }
        # Reuse canonical receipt creation inside the same transaction. This temporary
        # state is never committed or visible to a dispatcher.
        Action.update(state="dispatching").where(Action.id == action_id).execute()
        settle(action_id, outcome, result)
        emit(
            action.run_id,
            "action.reconciled",
            {
                "action_id": action_id,
                "operator": operator,
                "note": note,
                "previous_evidence": snapshot,
                "outcome": outcome,
                "observation": observation,
            },
        )
        # Preserve the original run failure and its events as historical evidence.
        return inspect_unknown(action_id)
