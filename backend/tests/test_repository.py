import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.db.database import initialize
from app.db.models import Action, Confirmation, Conversation, Event, Message, Run, Trace, db
from app.db.repository import accept, claim, cleanup, emit, recover, stop


def action(user):
    result = accept(user, "book Alice", str(uuid4()))
    return Action.create(
        run=result["run_id"],
        revision=result["revision"],
        generation=1,
        kind="create_booking",
        inputs="{}",
        fingerprint="digest",
    )


def test_double_submit_and_correction_claim_order(account):
    user, _ = account
    request_id = str(uuid4())
    first = accept(user, "hello", request_id)
    assert accept(user, "hello", request_id) == first
    assert Conversation.get(Conversation.user == user).revision == 1
    old = action(user)
    accept(user, "wait, Friday", str(uuid4()))
    assert not claim(old.id)
    current = action(user)
    assert claim(current.id)
    accept(user, "wait, Monday", str(uuid4()))
    assert Action.get_by_id(current.id).state == "dispatching"
    assert not claim(current.id)
    assert not claim(action(user).id)  # unresolved earlier dispatch blocks later mutation


def test_competing_executors_single_winner(account):
    user, _ = account
    item = action(user)
    gate = threading.Barrier(2)

    def worker():
        with db.connection_context():
            gate.wait()
            return claim(item.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [False, True]


def test_confirmation_consumed_once_and_stop(account):
    user, _ = account
    item = action(user)
    item.state = "awaiting_confirmation"
    item.save()
    Confirmation.create(
        conversation=item.run.conversation_id, action=item, expires=time.time() + 600
    )
    assert not claim(item.id)
    accepted = accept(user, "yes", str(uuid4()))
    assert Action.get_by_id(item.id).run_id == accepted["run_id"]
    assert claim(item.id)
    assert not claim(item.id)
    assert Confirmation.get(Confirmation.action == item).consumed
    newer = action(user)
    stop(newer.run_id)
    assert not claim(newer.id)


def test_unknown_recovery_no_resend_and_durable_events(account):
    user, _ = account
    item = action(user)
    Run.update(status="running").where(Run.id == item.run_id).execute()
    assert claim(item.id)
    emit(item.run_id, "tool.status", {"text": "dispatch"})
    recover()
    assert Action.get_by_id(item.id).state == "unknown"
    assert Run.get_by_id(item.run_id).status == "needs_reconciliation"
    assert not claim(item.id)
    assert [e.sequence for e in Event.select().where(Event.run == item.run_id)] == [1, 2]
    assert json.loads(Event.select().where(Event.kind == "run.failed").get().payload)["message"]


def test_cleanup_preserves_canonical_evidence_and_migration_idempotence(account):
    user, _ = account
    item = action(user)
    Trace.create(run=item.run_id, kind="model", payload="{}", created_at=time.time() - 31 * 86400)
    assert cleanup() == 1
    assert Action.get_by_id(item.id)
    assert Message.select().count() == 1
    path = db.database
    db.close()
    initialize(path)
    assert Run.select().count() == 1


def test_credential_generation_gate_and_no_credential_loss_after_claim(account):
    import pytest
    from fastapi import HTTPException

    from app.api.connection import change
    from app.db.models import Connection

    user, _ = account
    item = action(user)
    change(user.id)
    assert not claim(item.id)
    Connection.update(ciphertext="opaque", owner=42, generation=5).execute()
    next_item = action(user)
    next_item.generation = 5
    next_item.save()
    assert claim(next_item.id)
    with pytest.raises(HTTPException) as exc:
        change(user.id)
    assert exc.value.status_code == 409
    assert Connection.get(Connection.user == user).ciphertext == "opaque"
