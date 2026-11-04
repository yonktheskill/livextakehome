"""Deterministic semantic-tool contracts; no external model or Cal.com calls."""

import json

import pytest

from app.db.dispatch import inspect_unknown, prepare_action, resolve_unknown, settle
from app.db.models import Action, BookingRequest, Connection, Run
from app.db.repository import accept, claim, mark_uncertain, recover, stop
from app.db.requests import (
    RequestConflict,
    execute_request_tool,
    request_context,
    resolve_requests,
    task_write_plan,
)


def incoming(user, text):
    accepted = accept(user, text, text)
    run = Run.get_by_id(accepted["run_id"])
    run.status = "running"
    run.save()
    return run


def resolve(run, relation, task=None, facts=None, message_id=None):
    row = {"relation": relation, "reason": "Semantic relation grounded in original message"}
    if task:
        current = BookingRequest.get_by_id(task["request_id"])
        row.update(request_id=current.id, expected_revision=current.revision)
    if facts is not None:
        row["facts"] = facts
    return resolve_requests(run.id, message_id or run.message_id, [row])


def new(user):
    run = incoming(user, "Book Alex tomorrow at 10 for 30 minutes")
    task = resolve(
        run,
        "new",
        facts={
            "start": "2026-11-06T10:00:00+08:00",
            "duration_minutes": 30,
            "attendee": {"name": "Alex", "email": "alex@example.com"},
        },
    )[0]
    return run, task


def prepare(run, task):
    current = BookingRequest.get_by_id(task["request_id"])
    plan = task_write_plan(run.id, current.id, current.revision)
    args = (
        dict(plan["facts"])
        if plan["kind"] == "create_booking"
        else {
            "booking_uid": plan["booking_uid"],
            "new_start": plan["facts"]["start"],
        }
    )
    conn = Connection.get(Connection.user == run.conversation.user_id)
    action_id, _ = prepare_action(
        run,
        conn,
        plan["kind"],
        json.dumps(args, sort_keys=True),
        "fp",
        False,
        {},
        current.id,
        current.revision,
    )
    return action_id


def test_two_rapid_independent_messages_survive_and_gate_writes(account):
    user, _ = account
    first = incoming(user, "Book Alex at 10")
    second = incoming(user, "Also book Blair at 11")
    ctx = request_context(second.id)
    assert [i["message_id"] for i in ctx["inbox"]] == [first.message_id, second.message_id]
    with pytest.raises(RequestConflict, match="older"):
        resolve(second, "new", facts={"start": "11"})
    one = resolve(second, "new", facts={"start": "10"}, message_id=first.message_id)[0]
    with pytest.raises(RequestConflict, match="every pending"):
        task_write_plan(second.id, one["request_id"], 1)
    two = resolve(second, "new", facts={"start": "11"})[0]
    assert one["request_id"] != two["request_id"]
    assert len(request_context(second.id)["tasks"]) == 2
    assert task_write_plan(second.id, one["request_id"], 1)["kind"] == "create_booking"


def test_correction_before_claim_books_once_and_preserves_facts(account):
    user, _ = account
    run, task = new(user)
    stale = prepare(run, task)
    correction = incoming(user, "Let's book this for 12pm instead")
    amended = resolve(correction, "amend", task, {"start": "2026-11-06T12:00:00+08:00"})[0]
    assert amended["facts"]["duration_minutes"] == 30
    assert amended["facts"]["attendee"]["email"] == "alex@example.com"
    assert not claim(stale)
    action = prepare(correction, amended)
    assert claim(action)
    assert settle(action, "succeeded", {"response": {"uid": "actual"}})
    assert BookingRequest.get_by_id(task["request_id"]).state == "succeeded"
    with pytest.raises(RequestConflict):
        prepare(correction, amended)


@pytest.mark.parametrize("settle_first", [False, True])
def test_correction_after_claim_reschedules_actual_uid_and_chains(account, settle_first):
    user, _ = account
    run, task = new(user)
    action = prepare(run, task)
    assert claim(action)
    if settle_first:
        settle(action, "succeeded", {"response": {"uid": "actual"}})
    correction = incoming(user, "Move that to noon")
    resolve(correction, "amend", task, {"start": "noon"})
    if not settle_first:
        with pytest.raises(RequestConflict):
            prepare(correction, task)
        settle(action, "succeeded", {"response": {"uid": "actual"}})
    moved = prepare(correction, task)
    assert Action.get_by_id(moved).kind == "reschedule_booking"
    assert claim(moved)
    settle(moved, "succeeded", {"response": {"uid": "replacement"}})
    again = incoming(user, "Actually one hour later")
    resolve(again, "amend", task, {"start": "13:00"})
    plan = task_write_plan(again.id, task["request_id"], 3)
    assert plan["booking_uid"] == "replacement"
    assert plan["kind"] == "reschedule_booking"


def test_unknown_holds_corrections_until_operator_reconciles(account):
    user, _ = account
    run, task = new(user)
    action = prepare(run, task)
    assert claim(action)
    mark_uncertain(run.id)
    run.status = "needs_reconciliation"
    run.save()
    correction = incoming(user, "Move Alex to noon")
    resolve(correction, "amend", task, {"start": "noon"})
    with pytest.raises(RequestConflict):
        prepare(correction, task)
    snapshot = inspect_unknown(action)
    resolve_unknown(
        action, snapshot["digest"], "succeeded", "operator", "verified", {"uid": "real"}
    )
    assert task_write_plan(correction.id, task["request_id"], 2)["booking_uid"] == "real"


def test_definitive_failure_allows_corrected_create(account):
    user, _ = account
    run, task = new(user)
    action = prepare(run, task)
    assert claim(action)
    correction = incoming(user, "Move Alex to noon")
    resolve(correction, "amend", task, {"start": "noon"})
    settle(action, "failed", {"error": "definitive rejection"})
    assert task_write_plan(correction.id, task["request_id"], 2)["kind"] == "create_booking"


def test_ambiguity_holds_only_affected_task_and_clarification_persists(account):
    user, _ = account
    run, task = new(user)
    second = resolve_requests(
        run.id,
        run.message_id,
        [
            {
                "relation": "new",
                "reason": "Semantic relation grounded in original message",
                "facts": task["facts"],
            },
        ],
    )  # exact retry idempotent
    assert second[0]["request_id"] == task["request_id"]
    clarification = incoming(user, "Alex at noon")
    held = resolve(clarification, "clarify", task)[0]
    assert held["state"] == "held"
    with pytest.raises(RequestConflict):
        prepare(clarification, task)
    reply = incoming(user, "Move the existing meeting please")
    resolve(reply, "amend", held, {"start": "noon"})
    assert task_write_plan(reply.id, task["request_id"], 3)["kind"] == "create_booking"
    question = incoming(user, "What time zone is this?")
    resolve(question, "dismiss")
    assert len(request_context(question.id)["tasks"]) == 1
    assert task_write_plan(question.id, task["request_id"], 3)["facts"]["start"] == "noon"


def test_owner_scope_and_revision_validation(account):
    from app.db.models import Conversation, User

    user, _ = account
    _, task = new(user)
    other = User.create(email="other@example.com", password_hash="unused")
    Conversation.create(user=other)
    run = incoming(other, "Move Alex")
    with pytest.raises(RequestConflict, match="Unknown request"):
        resolve(run, "amend", task, {"start": "noon"})
    assert request_context(run.id)["tasks"] == []


@pytest.mark.parametrize("operation", ["stop", "recover"])
def test_no_blind_replay_stopped_or_recovered_task(account, operation):
    user, _ = account
    run, task = new(user)
    prepare(run, task)
    if operation == "stop":
        stop(run.id)
    else:
        recover()
    later = incoming(user, "Thanks")
    resolve(later, "dismiss")
    with pytest.raises(RequestConflict):
        prepare(later, task)
    with pytest.raises(RequestConflict, match="Explicit resume"):
        resolve(incoming(user, "Alex at noon"), "amend", task, {"start": "noon"})


def test_tool_execution_and_withdraw(account):
    user, _ = account
    run, task = new(user)
    assert execute_request_tool(run.id, "request_context", {})["tasks"]
    later = incoming(user, "Never mind Alex")
    resolve(later, "withdraw", task)
    with pytest.raises(RequestConflict):
        prepare(later, task)


def test_accept_preserves_other_task_proposals_until_semantic_resolution(account):
    user, _ = account
    run, task = new(user)
    action = prepare(run, task)
    second = incoming(user, "Also book Blair")
    resolve(second, "new", facts={"start": "11"})
    assert Action.get_by_id(action).state == "prepared"
    assert BookingRequest.get_by_id(task["request_id"]).revision == 1
    correction = incoming(user, "Move Alex")
    resolve(correction, "amend", task, {"start": "12"})
    assert Action.get_by_id(action).state == "superseded"


def test_migrate_v4_booking_linkage_is_conservative_and_idempotent(account):
    from app.db.database import initialize
    from app.db.models import RequestInbox, SchemaVersion, db

    user, _ = account
    run = incoming(user, "legacy booking")
    action = Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="create_booking",
        inputs='{"start":"10"}',
        fingerprint="fp",
        state="succeeded",
        result='{"response":{"uid":"old"}}',
    )
    RequestInbox.delete().execute()
    SchemaVersion.delete().execute()
    SchemaVersion.create(version=4)
    path = db.database
    db.close()
    initialize(path)
    with db.connection_context():
        restored = Action.get_by_id(action.id)
        assert restored.task_id
        assert restored.task.state == "succeeded"
        assert restored.task.booking_uid == "old"
        assert restored.task.facts == '{"start": "10"}'
    initialize(path)
    with db.connection_context():
        assert BookingRequest.select().count() == 1
        assert RequestInbox.select().count() == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("request_context", {"offset": True}),
        ("request_context", {"extra": 1}),
        ("request_context", {"limit": "5"}),
        ("resolve_requests", {}),
        ("resolve_requests", {"message_id": "x", "resolutions": [None]}),
        ("resolve_requests", {"message_id": "x", "resolutions": [{"relation": []}]}),
        (
            "resolve_requests",
            {
                "message_id": "x",
                "resolutions": [{"relation": "new", "reason": "booking", "request_id": "foreign"}],
            },
        ),
    ],
)
def test_tool_inputs_reject_unknown_missing_and_wrong_types(account, name, args):
    user, _ = account
    run = incoming(user, "Book Alex")
    with pytest.raises(RequestConflict):
        execute_request_tool(run.id, name, args)


def test_first_uninterpreted_message_cannot_prepare_or_claim_legacy_action(account):
    user, _ = account
    run = incoming(user, "Book Alex")
    conn = Connection.get(Connection.user == user)
    with pytest.raises(RequestConflict, match="every pending"):
        prepare_action(run, conn, "create_booking", "{}", "fp", False)
    action = Action.create(
        run=run,
        revision=run.revision,
        generation=1,
        kind="create_booking",
        inputs="{}",
        fingerprint="fp",
    )
    assert not claim(action.id)


def test_no_success_with_unresolved_inbox_or_actionable_task(account):
    from app.db.repository import finish

    user, _ = account
    run = incoming(user, "Book Alex")
    finish(run.id, "succeeded", "run.succeeded", {})
    assert Run.get_by_id(run.id).status == "waiting_for_user"
    run.status = "running"
    run.save()
    resolve(run, "new", facts={"start": "10"})
    finish(run.id, "succeeded", "run.succeeded", {})
    assert Run.get_by_id(run.id).status == "waiting_for_user"
