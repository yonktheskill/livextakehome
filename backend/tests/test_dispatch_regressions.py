import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.db.dispatch import StalePreparation, prepare_action, settle
from app.db.models import Action, Confirmation, Connection, Event, Message, Run, db
from app.db.repository import accept, claim


def proposal(user, content="cancel"):
    accepted = accept(user, content, str(uuid4()))
    return Run.get_by_id(accepted["run_id"]), Connection.get(Connection.user == user)


def prepare(run, conn, confirmation=True):
    return prepare_action(
        run,
        conn,
        "cancel_booking",
        '{"booking_uid":"abc"}',
        "fingerprint",
        confirmation,
        {
            "method": "POST",
            "path": "/v2/bookings/abc/cancel",
            "payload": {},
            "version": "2026-02-25",
            "validation": {"uid": "abc"},
        },
    )


def test_late_preparation_after_correction_is_rejected(account):
    user, _ = account
    stale, conn = proposal(user)
    accept(user, "No, keep that booking", str(uuid4()))
    with pytest.raises(StalePreparation):
        prepare(stale, conn)
    accept(user, "yes", str(uuid4()))
    assert Action.select().count() == Confirmation.select().count() == 0


def test_confirmation_only_answers_immediately_previous_proposal(account):
    user, _ = account
    old, conn = proposal(user)
    action_id, _ = prepare(old, conn)
    accept(user, "No, keep it", str(uuid4()))
    # Defense in depth: even a late legacy proposal cannot be revived by yes.
    Action.update(state="awaiting_confirmation").where(Action.id == action_id).execute()
    accept(user, "yes", str(uuid4()))
    assert not claim(action_id)
    assert Action.get_by_id(action_id).state == "superseded"


def test_cancellation_phrase_cannot_authorize_type_creation(account):
    user, _ = account
    run, conn = proposal(user)
    action_id, _ = prepare_action(run, conn, "create_event_type", "{}", "fp", True)
    accept(user, "confirm cancellation", str(uuid4()))
    assert not claim(action_id)
    assert not Confirmation.get(Confirmation.action == action_id).consumed


def test_competing_preparations_share_one_dispatch_identity(account):
    user, _ = account
    run, conn = proposal(user)
    barrier = threading.Barrier(2)

    def worker():
        with db.connection_context():
            barrier.wait()
            return prepare(run, conn, False)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        ids = [f.result() for f in futures]
    assert ids[0] == ids[1] and Action.select().count() == 1
    assert claim(ids[0])
    assert settle(ids[0], "succeeded", {"response": {"uid": "abc", "status": "cancelled"}})
    assert not claim(ids[1])


def test_settlement_and_canonical_receipt_are_atomic(account, monkeypatch):
    user, _ = account
    run, conn = proposal(user)
    action_id, _ = prepare(run, conn, False)
    frozen = json.loads(Action.get_by_id(action_id).request)
    assert frozen["path"] == "/v2/bookings/abc/cancel"
    assert frozen["version"] == "2026-02-25"
    assert claim(action_id)
    import app.db.dispatch as dispatch

    original = dispatch.emit

    def fail(*args):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(dispatch, "emit", fail)
    result = {"response": {"uid": "abc", "status": "cancelled"}}
    with pytest.raises(RuntimeError):
        settle(action_id, "succeeded", result)
    assert Action.get_by_id(action_id).state == "dispatching"
    assert Event.select().count() == 0
    monkeypatch.setattr(dispatch, "emit", original)
    assert settle(action_id, "succeeded", result)
    assert not settle(action_id, "unknown", {})
    assert Action.get_by_id(action_id).state == "succeeded"
    assert Event.select().where(Event.kind == "action.result").count() == 1
    assert Message.get(Message.client_request_id == f"action:{action_id}").content


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            {"status": "accepted", "start": "2026-11-06T04:00:00Z"},
            "Friday, 6 November 2026 at 12:00 (Asia/Singapore)",
        ),
        ({"status": "pending"}, "Sent the reschedule request"),
        ({"status": "accepted", "start": "Unavailable"}, "Unavailable"),
    ],
)
def test_reschedule_chat_uses_returned_status_and_times_without_losing_settlement(
    account, response, expected
):
    user, _ = account
    user.time_zone = "Asia/Singapore"
    user.save()
    run, conn = proposal(user, "reschedule")
    action_id, _ = prepare_action(run, conn, "reschedule_booking", "{}", "fp", False)
    assert claim(action_id)
    assert settle(
        action_id,
        "succeeded",
        {
            "response": {"uid": "abc", "title": "Intro", **response},
            "validated": {"start": "2026-11-05T04:00:00Z", "status": "accepted"},
        },
    )
    assert Action.get_by_id(action_id).state == "succeeded"
    message = Message.get(Message.client_request_id == f"action:{action_id}")
    assert expected in message.content
    assert "5 November" not in message.content
    assert "2026-11-05" not in message.content
    if response["status"] == "pending":
        assert "Rescheduled" not in message.content
        assert "**pending**" in message.content
    delta = Event.get(Event.kind == "message.delta")
    assert json.loads(delta.payload)["text"] == message.content
