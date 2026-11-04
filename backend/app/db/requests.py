"""Durable, owner-scoped semantic inbox. No keyword classification or inference here.

The ordinary agent resolves each original message through resolve_requests. The DB
validates identity, revisions and safety; the model supplies the semantic relation.
All public sync APIs run inside the caller's connection (use database.query).
"""

import json
from datetime import datetime

from app.core.guardrails import require_content, safe_data
from app.db.image_context import ambiguous_fields, source_images
from app.db.models import Action, BookingRequest, Message, RequestInbox, Run, db


class RequestConflict(ValueError):
    pass


BLOCKED = {"held", "withdrawn", "stopped", "recovery_blocked", "unknown", "failed"}


def _run(run_id):
    run = Run.get_by_id(run_id)
    if run.interrupted or run.status not in {"running", "queued"}:
        raise RequestConflict("Run is inactive; do not resume stopped work")
    if run.revision != run.conversation.revision:
        raise RequestConflict("New incoming messages must be interpreted first")
    return run


def _task(run, request_id):
    task = BookingRequest.get_or_none(
        (BookingRequest.id == request_id) & (BookingRequest.conversation == run.conversation_id)
    )
    if task is None:
        raise RequestConflict("Unknown request in this conversation")
    return task


def _image_sources(task):
    facts = json.loads(task.facts)
    if "_image_ambiguity_sources" in facts:
        return facts["_image_ambiguity_sources"]
    return (
        [task.origin_message_id]
        if ambiguous_fields(task.origin_message_id) and not facts.get("_image_ambiguity_resolved")
        else []
    )


def _view(task):
    return {
        "request_id": task.id,
        "revision": task.revision,
        "state": task.state,
        "facts": json.loads(task.facts),
        "booking_uid": task.booking_uid,
        "origin_message_id": task.origin_message_id,
        "original_user_message": task.origin_message.content,
        **source_images(task.origin_message_id),
        **(
            {
                "image_ambiguities": [
                    {"source_message_id": key, "fields": ambiguous_fields(key)}
                    for key in _image_sources(task)
                ]
            }
            if _image_sources(task)
            else {}
        ),
        "last_action_id": task.last_action_id,
    }


def request_context(run_id, limit=20, offset=0):
    """Bounded prompt facts; IDs refer only to this authenticated conversation.

    Unresolved inbox is paginated oldest-first. Resolve it then fetch again; writes
    stay blocked until *all* messages through this run's revision are resolved.
    """
    run = Run.get_by_id(run_id)
    limit = max(1, min(limit, 40))
    offset = max(0, int(offset))
    inbox = list(
        RequestInbox.select()
        .join(Message)
        .join(Run, on=(Run.message == Message.id))
        .where(
            (RequestInbox.conversation == run.conversation_id)
            & (RequestInbox.state == "pending")
            & (Run.revision <= run.revision)
        )
        .order_by(Run.revision)
        .limit(limit + 1)
    )
    tasks = list(
        BookingRequest.select()
        .where(BookingRequest.conversation == run.conversation_id)
        .order_by(
            BookingRequest.state.in_(["pending", "held", "settling", "unknown"]).desc(),
            BookingRequest.created_at.desc(),
            BookingRequest.id,
        )
        .offset(offset)
        .limit(limit + 1)
    )
    return safe_data(
        {
            "tasks": [_view(t) for t in tasks[:limit]],
            "tasks_truncated": len(tasks) > limit,
            "next_offset": offset + limit if len(tasks) > limit else None,
            "inbox": [
                {
                    "message_id": i.message_id,
                    "content": i.message.content,
                    **source_images(i.message_id),
                    "time_zone": i.message.time_zone,
                }
                for i in inbox[:limit]
            ],
            "inbox_truncated": len(inbox) > limit,
        }
    )


def assert_requests_ready(run_id):
    run = _run(run_id)
    if (
        RequestInbox.select()
        .where(
            (RequestInbox.conversation == run.conversation_id) & (RequestInbox.state == "pending")
        )
        .exists()
    ):
        raise RequestConflict("Interpret every pending incoming message before any write")
    return run


def _invalidate(task):
    Action.update(state="superseded", logical_key=None).where(
        (Action.task == task.id)
        & Action.state.in_(["prepared", "authorized", "awaiting_confirmation"])
    ).execute()


def _merge(base, patch):
    """Partial amendments preserve unstated facts, including nested attendee fields."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


def _image_clarification(run, origin, blockers, evidence):
    if not isinstance(evidence, dict):
        raise RequestConflict("Supply original text evidence for image ambiguity")
    for field in blockers:
        item = evidence.get(field, {})
        if not isinstance(item, dict):
            raise RequestConflict("Supply field clarification evidence")
        source = Message.get_or_none(
            (Message.id == item.get("source_message_id"))
            & (Message.conversation == run.conversation_id)
            & (Message.role == "user")
            & (Message.created_at <= run.message.created_at)
        )
        quote = item.get("quote")
        if (
            not source
            or not isinstance(quote, str)
            or not quote.strip()
            or quote not in source.content
        ):
            raise RequestConflict(
                "Each ambiguous image field requires exact outer user text evidence"
            )


def resolve_requests(run_id, message_id, resolutions):
    """Resolve ONE original inbox message atomically; retry is idempotent.

    Each resolution: relation=new|amend|clarify|withdraw|resume|dismiss,
    request_id + expected_revision for existing tasks, facts (new/partial amend),
    reason grounded in the original user text. Multiple new requests per message
    are supported. clarify holds each named ambiguous task until a later explicit
    amend/resume/withdraw. dismiss means no task change (questions/confirmations).
    """
    _validate_resolutions(resolutions)
    require_content(resolutions)
    encoded = json.dumps(resolutions, sort_keys=True)
    if len(encoded) > 40000:
        raise RequestConflict("Resolution too large")
    with db.atomic("IMMEDIATE"):
        run = _run(run_id)
        inbox = RequestInbox.get_or_none(
            (RequestInbox.message == message_id)
            & (RequestInbox.conversation == run.conversation_id)
        )
        if not inbox:
            raise RequestConflict("Unknown original user message")
        if inbox.state == "resolved":
            previous = json.loads(inbox.resolution)
            if previous["input"] != resolutions:
                raise RequestConflict("Message already interpreted differently")
            return previous["result"]
        oldest = (
            RequestInbox.select()
            .join(Message)
            .join(Run, on=(Run.message == Message.id))
            .where(
                (RequestInbox.conversation == run.conversation_id)
                & (RequestInbox.state == "pending")
            )
            .order_by(Run.revision)
            .first()
        )
        if oldest is None or oldest.id != inbox.id:
            raise RequestConflict("Resolve older pending messages first")
        result = []
        for resolution in resolutions:
            relation = resolution.get("relation")
            if not isinstance(resolution.get("reason"), str) or not resolution["reason"].strip():
                raise RequestConflict("Semantic resolution requires a reason")
            facts = resolution.get("facts", {})
            if not isinstance(facts, dict):
                raise RequestConflict("facts must be an object")
            if relation == "new":
                if resolution.get("request_id"):
                    raise RequestConflict("New requests cannot reuse a task ID")
                blockers = ambiguous_fields(message_id)
                if blockers and resolution.get("clarification_evidence"):
                    _image_clarification(
                        run, message_id, blockers, resolution["clarification_evidence"]
                    )
                    facts = {**facts, "_image_ambiguity_resolved": True}
                    blockers = []
                if blockers:
                    facts = {**facts, "_image_ambiguity_sources": [message_id]}
                task = BookingRequest.create(
                    conversation=run.conversation_id,
                    origin_message=message_id,
                    facts=json.dumps(facts, sort_keys=True),
                    state="held" if blockers else "pending",
                )
            elif relation == "dismiss":
                if resolution.get("request_id") or facts:
                    raise RequestConflict("Dismiss cannot change a request")
                continue
            elif relation in {"amend", "clarify", "withdraw", "resume"}:
                task = _task(run, resolution.get("request_id"))
                if resolution.get("expected_revision") != task.revision:
                    raise RequestConflict("Request revision changed; read context again")
                if relation in {"amend", "resume"} and task.state in {
                    "stopped",
                    "recovery_blocked",
                    "withdrawn",
                }:
                    # Explicit resume is required; an incidental amendment never revives Stop.
                    if relation != "resume":
                        raise RequestConflict("Explicit resume required for blocked request")
                if relation != "amend" and facts:
                    raise RequestConflict("Only amendments may change existing facts")
                pending_image_sources = _image_sources(task)
                if relation in {"amend", "resume"}:
                    incoming_ambiguity = bool(ambiguous_fields(message_id))
                    if incoming_ambiguity and message_id not in pending_image_sources:
                        pending_image_sources.append(message_id)
                    blockers = sorted(
                        {field for key in pending_image_sources for field in ambiguous_fields(key)}
                    )
                    if blockers and (
                        resolution.get("clarification_evidence") or not incoming_ambiguity
                    ):
                        _image_clarification(
                            run,
                            task.origin_message_id,
                            blockers,
                            resolution.get("clarification_evidence", {}),
                        )
                        pending_image_sources = []
                    if blockers or "_image_ambiguity_sources" in json.loads(task.facts):
                        task.facts = json.dumps(
                            {
                                **json.loads(task.facts),
                                "_image_ambiguity_sources": pending_image_sources,
                            }
                        )
                _invalidate(task)
                task.revision += 1
                if relation == "amend":
                    task.facts = json.dumps(_merge(json.loads(task.facts), facts), sort_keys=True)
                task.state = {"clarify": "held", "withdraw": "withdrawn"}.get(relation, "pending")
                if relation in {"amend", "resume"} and pending_image_sources:
                    task.state = "held"
                action = Action.get_or_none(Action.id == task.last_action_id)
                if relation in {"amend", "resume"} and action:
                    if action.state in {"dispatching", "unknown"}:
                        task.state = "settling" if action.state == "dispatching" else "unknown"
                task.save()
            else:
                raise RequestConflict("Unknown semantic relation")
            result.append(_view(task))
        inbox.state = "resolved"
        inbox.resolution = json.dumps({"input": resolutions, "result": result})
        inbox.resolved_run_id = run.id
        inbox.save()
        return result


def task_write_plan(run_id, request_id, expected_revision):
    """Call BEFORE live validation/preparation. Reread UID before a reschedule.

    The returned kind is mandatory: never issue a second create for a task whose
    earlier create succeeded, even if the amendment arrived during dispatch.
    """
    run = assert_requests_ready(run_id)
    task = _task(run, request_id)
    if task.revision != expected_revision:
        raise RequestConflict("Request revision changed")
    if task.state in BLOCKED or task.state in {"settling", "succeeded"}:
        raise RequestConflict(f"Request is {task.state}; no write permitted")
    action = Action.get_or_none(Action.id == task.last_action_id)
    if action and action.state in {"dispatching", "unknown"}:
        raise RequestConflict("Prior dispatch must settle/reconcile first")
    return {
        **_view(task),
        "kind": "reschedule_booking" if task.booking_uid else "create_booking",
        "must_reread_booking": bool(task.booking_uid),
    }


def associate_action(run, name, encoded, request_id, request_revision):
    """Internal prepare gate, in the same IMMEDIATE transaction as Action.create."""
    plan = task_write_plan(run.id, request_id, request_revision)
    inputs = json.loads(encoded)
    if name == "create_event_type" and plan["kind"] == "create_booking":
        agreed = plan["facts"].get("duration_minutes")
        if agreed is not None and agreed != inputs.get("duration_minutes"):
            raise RequestConflict("Reusable type must preserve the task's agreed duration")
        return BookingRequest.get_by_id(request_id)
    if name != plan["kind"]:
        raise RequestConflict(f"This task requires {plan['kind']}")
    if name == "reschedule_booking" and inputs.get("booking_uid") != plan["booking_uid"]:
        raise RequestConflict("Reschedule must target the task's actual resulting UID")
    # Ground dispatch in preserved facts, not a stale/reconstructed tool call.
    facts = plan["facts"]
    if name == "reschedule_booking":
        original = (
            Action.select()
            .where(
                (Action.task == request_id)
                & (Action.kind == "create_booking")
                & (Action.state == "succeeded")
            )
            .order_by(Action.created_at.desc())
            .first()
        )
        if original:
            booked = json.loads(original.inputs)
            for field in (
                "attendee",
                "duration_minutes",
                "event_type_id",
                "booking_fields",
                "location",
            ):
                if (
                    field in facts
                    and field in booked
                    and not facts_agree(facts[field], booked[field])
                ):
                    raise RequestConflict(
                        f"Rescheduling preserves {field}; clarify a supported replacement workflow"
                    )
    for field in (
        "start",
        "attendee",
        "duration_minutes",
        "event_type_id",
        "explicit_end",
        "maximum_minutes",
        "booking_fields",
        "location",
    ):
        if field not in facts:
            continue
        target = "new_start" if name == "reschedule_booking" and field == "start" else field
        if name == "create_booking" or target == "new_start":
            if not facts_agree(facts[field], inputs.get(target), field):
                raise RequestConflict(f"Tool input disagrees with current request fact: {field}")
    return BookingRequest.get_by_id(request_id)


def facts_agree(expected, actual, field=""):
    """Preserved partial facts allow resolved fields and equivalent timezone offsets."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and facts_agree(v, actual[k], k) for k, v in expected.items()
        )
    if field in {"start", "new_start", "explicit_end"} and isinstance(expected, str):
        try:
            left = datetime.fromisoformat(expected.replace("Z", "+00:00"))
            right = datetime.fromisoformat(actual.replace("Z", "+00:00"))
            return left.tzinfo is not None and right.tzinfo is not None and left == right
        except (ValueError, TypeError, AttributeError):
            pass
    return expected == actual


def task_claimable(action):
    try:
        assert_requests_ready(action.run_id)
        if not action.task_id:
            return True  # Legacy action still requires an interpreted inbox and live run.
        plan = task_write_plan(action.run_id, action.task_id, action.task_revision)
        if action.kind == "create_event_type":
            return plan["kind"] == "create_booking"
        task = BookingRequest.get_by_id(action.task_id)
        return plan["kind"] == action.kind and task.last_action_id == action.id
    except RequestConflict:
        return False


def settle_task(action):
    """Called with canonical settlement transaction; retains corrections in flight."""
    if not action.task_id or action.kind == "create_event_type":
        return
    task = BookingRequest.get_by_id(action.task_id)
    if task.last_action_id != action.id:
        return
    if action.state == "succeeded":
        response = json.loads(action.result).get("response", {})
        resulting_uid = response.get("uid")
        if resulting_uid:
            task.booking_uid = resulting_uid
        else:
            task.state = "unknown"  # success without resource identity cannot be replayed
            task.save()
            return
        if task.state not in {"held", "withdrawn", "stopped", "recovery_blocked"}:
            task.state = "succeeded" if task.revision == action.task_revision else "pending"
    elif action.state == "failed":
        if task.state not in {"held", "withdrawn", "stopped", "recovery_blocked"}:
            task.state = "failed" if task.revision == action.task_revision else "pending"
    elif action.state == "unknown" and task.state not in {"withdrawn", "stopped", "held"}:
        task.state = "unknown"
    task.save()


def block_run_requests(run_id, state):
    """Stop/recovery never silently make durable tasks replayable."""
    ids = Action.select(Action.task).where((Action.run == run_id) & Action.task.is_null(False))
    run = Run.get_by_id(run_id)
    resolved = RequestInbox.select().where(RequestInbox.resolved_run_id == run_id)
    task_ids = {
        row["request_id"]
        for inbox in resolved
        for row in json.loads(inbox.resolution).get("result", [])
    }
    task_ids.update(a.task_id for a in ids)
    if task_ids:
        BookingRequest.update(state=state).where(
            (BookingRequest.conversation == run.conversation_id)
            & BookingRequest.id.in_(task_ids)
            & BookingRequest.state.not_in(["succeeded", "withdrawn", "unknown"])
        ).execute()


def request_tool_definitions():
    return [
        {
            "type": "function",
            "function": {
                "name": "request_context",
                "description": (
                    "Read durable requests and pending original messages; "
                    "paginate tasks via offset."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "resolve_requests",
                "description": (
                    "Interpret original pending user messages oldest first. Preserve independent "
                    "requests. Amend only a unique referent with partial facts; clarify holds "
                    "ambiguous tasks. Use dismiss for unrelated questions or confirmations. "
                    "Never infer relations from keywords alone."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "resolutions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 40,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "relation": {
                                        "type": "string",
                                        "enum": [
                                            "new",
                                            "amend",
                                            "clarify",
                                            "withdraw",
                                            "resume",
                                            "dismiss",
                                        ],
                                    },
                                    "request_id": {"type": "string"},
                                    "expected_revision": {"type": "integer", "minimum": 1},
                                    "facts": {
                                        "type": "object",
                                        "description": "Partial booking arguments: start "
                                        "(offset-aware ISO timestamp), attendee, duration_minutes, "
                                        "event_type_id, booking_fields, location, explicit_end or "
                                        "maximum_minutes. Preserve unstated fields. Resolve each "
                                        "message once; new is for appointments, dismiss for reads, "
                                        "profile, cancellation/type commands and approval replies.",
                                    },
                                    "clarification_evidence": {
                                        "type": "object",
                                        "description": "Map each ambiguous field to "
                                        "{source_message_id,quote}: exact original outer user text "
                                        "resolving it. Never quote image observations.",
                                    },
                                    "reason": {"type": "string"},
                                },
                                "required": ["relation", "reason"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["message_id", "resolutions"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def execute_request_tool(run_id, name, arguments):
    """Strict ordinary-tool boundary; malformed model arguments fail closed."""
    if not isinstance(arguments, dict):
        raise RequestConflict("Tool arguments must be an object")
    if name == "request_context":
        if set(arguments) - {"offset", "limit"}:
            raise RequestConflict("Unknown request_context argument")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
        if type(offset) is not int or offset < 0:
            raise RequestConflict("offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 40:
            raise RequestConflict("limit must be an integer from 1 to 40")
        return request_context(run_id, limit=limit, offset=offset)
    if name != "resolve_requests":
        raise RequestConflict("Unknown request tool")
    if set(arguments) != {"message_id", "resolutions"}:
        raise RequestConflict("resolve_requests requires exactly message_id and resolutions")
    if not isinstance(arguments["message_id"], str) or not arguments["message_id"]:
        raise RequestConflict("message_id must be a nonempty string")
    _validate_resolutions(arguments["resolutions"])
    return resolve_requests(run_id, arguments["message_id"], arguments["resolutions"])


def _validate_resolutions(resolutions):
    if not isinstance(resolutions, list) or not 1 <= len(resolutions) <= 40:
        raise RequestConflict("Supply 1..40 semantic resolutions")
    for row in resolutions:
        if not isinstance(row, dict) or set(row) - {
            "relation",
            "reason",
            "request_id",
            "expected_revision",
            "clarification_evidence",
            "facts",
        }:
            raise RequestConflict("Resolution contains unknown fields or is not an object")
        relation = row.get("relation")
        if not isinstance(relation, str) or relation not in {
            "new",
            "amend",
            "clarify",
            "withdraw",
            "resume",
            "dismiss",
        }:
            raise RequestConflict("Unknown semantic relation")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise RequestConflict("Semantic resolution requires a reason")
        if "facts" in row and not isinstance(row["facts"], dict):
            raise RequestConflict("facts must be an object")
        if relation in {"new", "dismiss"}:
            if "request_id" in row or "expected_revision" in row:
                raise RequestConflict("New/dismiss resolutions cannot target existing IDs")
        else:
            if not isinstance(row.get("request_id"), str) or not row["request_id"]:
                raise RequestConflict("Existing task resolution requires request_id")
            revision = row.get("expected_revision")
            if type(revision) is not int or revision < 1:
                raise RequestConflict("expected_revision must be a positive integer")
        if any(str(key).startswith("_image_") for key in row.get("facts", {})):
            raise RequestConflict("Reserved image provenance fields")
        if relation not in {"new", "amend"} and row.get("facts"):
            raise RequestConflict("Only new/amend resolutions may supply facts")


def backfill_legacy_requests():
    """Migration-only conservative linkage; never replay an old task automatically."""
    for action in Action.select().where((Action.kind == "create_booking") & Action.task.is_null()):
        inputs = json.loads(action.inputs)
        response = json.loads(action.result).get("response", {})
        state = "recovery_blocked"
        if action.state in {"dispatching", "unknown"}:
            state = "unknown"
        elif action.state == "succeeded":
            state = "succeeded" if response.get("uid") else "unknown"
        task = BookingRequest.create(
            conversation=action.run.conversation_id,
            origin_message=action.run.message_id,
            facts=json.dumps(inputs),
            state=state,
            booking_uid=response.get("uid"),
            last_action_id=action.id,
        )
        Action.update(task=task.id, task_revision=1).where(Action.id == action.id).execute()
    for run in Run.select():
        if run.message.role != "user":
            continue
        # Only unprocessed queued legacy messages need semantic interpretation.
        # All other historical messages remain available as original evidence,
        # without converting old instructions into fresh replayable work.
        pending = run.status == "queued" and not run.interrupted
        RequestInbox.get_or_create(
            message=run.message_id,
            defaults={
                "conversation": run.conversation_id,
                "state": "pending" if pending else "resolved",
                "resolution": json.dumps({"input": [], "result": [], "legacy": True}),
            },
        )


def request_completion_state(run_id):
    """A small truthful terminal-state guard for runner finalization."""
    run = Run.get_by_id(run_id)
    inbox_pending = (
        RequestInbox.select()
        .where(
            (RequestInbox.conversation == run.conversation_id) & (RequestInbox.state == "pending")
        )
        .count()
    )
    tasks_pending = (
        BookingRequest.select()
        .where(
            (BookingRequest.conversation == run.conversation_id)
            & BookingRequest.state.in_(["pending", "held", "settling", "unknown"])
        )
        .count()
    )
    return {
        "inbox_pending": inbox_pending,
        "tasks_pending": tasks_pending,
        "complete": not inbox_pending and not tasks_pending,
    }
