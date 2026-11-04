import asyncio
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.database import query
from app.db.dispatch import inspect_unknown, prepare_action, resolve_unknown
from app.db.models import Action, Connection, Event, Message, Run
from app.db.repository import accept, claim, finish, recover, wait_for_user
from app.main import RateBuckets, auth_address
from app.tools.messages import confirmation_message


def proposal(account, confirmation=True):
    user, _ = account
    run_id = accept(user, "cancel booking abc", str(uuid4()))["run_id"]
    Run.update(status="running").where(Run.id == run_id).execute()
    details = {"uid": "abc", "title": "Consultation"}
    action_id, _ = prepare_action(
        Run.get_by_id(run_id),
        Connection.get(Connection.user == user),
        "cancel_booking",
        '{"booking_uid":"abc"}',
        "fp",
        confirmation,
        {"path": "/v2/bookings/abc/cancel", "payload": {}, "validation": details},
    )
    return run_id, action_id, confirmation_message("cancel_booking", details, "UTC")


def test_unexposed_proposal_never_accepts_yes_even_after_recovery(account):
    run_id, action_id, _ = proposal(account)
    assert Action.get_by_id(action_id).state == "prepared"
    recover()
    accept(account[0], "yes", str(uuid4()))
    assert Action.get_by_id(action_id).state == "superseded"
    assert not claim(action_id)
    assert Run.get_by_id(run_id).status == "interrupted"


def test_atomic_exposure_survives_recovery_and_exact_confirmation(account):
    run_id, action_id, prompt = proposal(account)
    wait_for_user(run_id, prompt)
    recover()
    assert Run.get_by_id(run_id).status == "waiting_for_user"
    accepted = accept(account[0], "yes", str(uuid4()))
    assert not claim(action_id)  # queued runs have no execution lease
    Run.update(status="running").where(Run.id == accepted["run_id"]).execute()
    assert claim(action_id)
    frozen = json.loads(Action.get_by_id(action_id).request)
    assert frozen["confirmation_prompt"] == prompt
    assert frozen["dispatch_authorization"]["confirmation_message_id"] == accepted["message_id"]


def test_prompt_failure_rolls_back_activation(account, monkeypatch):
    import app.db.repository as repository

    run_id, action_id, prompt = proposal(account)
    monkeypatch.setattr(repository, "emit", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        wait_for_user(run_id, prompt)
    assert Run.get_by_id(run_id).status == "running"
    assert Action.get_by_id(action_id).state == "prepared"
    assert not Message.select().where(Message.role == "assistant").exists()


@pytest.mark.asyncio
async def test_cancelled_delayed_claim_drains_before_terminal_settlement(account):
    run_id, action_id, _ = proposal(account, False)
    started, release = threading.Event(), threading.Event()

    def delayed_claim():
        started.set()
        assert release.wait(5)
        return claim(action_id)

    task = asyncio.create_task(query(delayed_claim))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancellation must still drain the worker
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Terminal settlement observes the completed claim, never a future late claim.
    from app.db.repository import mark_uncertain

    assert mark_uncertain(run_id)
    finish(run_id, "needs_reconciliation", "run.failed", {})
    assert Action.get_by_id(action_id).state == "unknown"
    assert not claim(action_id)


def test_terminal_finish_invalidates_preparations(account):
    run_id, action_id, _ = proposal(account, False)
    finish(run_id, "failed", "run.failed", {})
    assert Action.get_by_id(action_id).state == "superseded"
    assert not claim(action_id)


def unknown(account):
    run_id, action_id, _ = proposal(account, False)
    assert claim(action_id)
    recover()
    return run_id, action_id


def test_reconcile_requires_frozen_evidence_and_writes_one_canonical_receipt(account):
    run_id, action_id = unknown(account)
    snapshot = inspect_unknown(action_id)
    with pytest.raises(ValueError, match="Evidence changed"):
        resolve_unknown(action_id, "wrong", "succeeded", "ops", "verified", {"uid": "abc"})
    with pytest.raises(ValueError, match="frozen target"):
        resolve_unknown(
            action_id, snapshot["digest"], "succeeded", "ops", "verified", {"uid": "other"}
        )
    resolve_unknown(
        action_id,
        snapshot["digest"],
        "succeeded",
        "ops",
        "Audit case 1",
        {
            "uid": "abc",
            "status": "cancelled",
        },
    )
    assert Action.get_by_id(action_id).state == "succeeded"
    assert Run.get_by_id(run_id).status == "needs_reconciliation"
    assert Message.get(Message.client_request_id == f"action:{action_id}").content
    assert Event.select().where(Event.kind == "action.result").count() == 1
    audit = json.loads(Event.get(Event.kind == "action.reconciled").payload)
    assert audit["previous_evidence"] == snapshot
    assert audit["operator"] == "ops"
    with pytest.raises(ValueError):
        resolve_unknown(action_id, snapshot["digest"], "failed", "ops", "retry", {"proof": "x"})


def test_reconciliation_audit_failure_rolls_back_receipt_and_resolution(account, monkeypatch):
    import app.db.dispatch as dispatch

    _, action_id = unknown(account)
    snapshot = inspect_unknown(action_id)
    original = dispatch.emit

    def fail_audit(run_id, kind, payload):
        if kind == "action.reconciled":
            raise RuntimeError("disk failure")
        return original(run_id, kind, payload)

    monkeypatch.setattr(dispatch, "emit", fail_audit)
    with pytest.raises(RuntimeError):
        resolve_unknown(
            action_id, snapshot["digest"], "succeeded", "ops", "verified", {"uid": "abc"}
        )
    assert Action.get_by_id(action_id).state == "unknown"
    assert not Message.select().where(Message.client_request_id == f"action:{action_id}").exists()
    assert not Event.select().where(Event.kind == "action.result").exists()


def test_verified_failure_unblocks_writes_without_resending(account):
    _, action_id = unknown(account)
    snapshot = inspect_unknown(action_id)
    resolve_unknown(
        action_id,
        snapshot["digest"],
        "failed",
        "ops",
        "Definite rejection",
        {
            "audit_reference": "server rejected before mutation",
        },
    )
    _, new_action, _ = proposal(account, False)
    assert claim(new_action)
    assert not Message.select().where(Message.client_request_id == f"action:{action_id}").exists()


def test_buckets_bound_memory_expire_idle_and_keep_active_quotas():
    buckets = RateBuckets(capacity=2)
    assert all(buckets.allow(("chat", "one"), 0) for _ in range(30))
    assert not buckets.allow(("chat", "one"), 1)
    assert buckets.allow(("chat", "two"), 1)
    assert not buckets.allow(("chat", "three"), 2)
    assert len(buckets.buckets) == 2
    assert buckets.allow(("chat", "three"), 61)
    assert len(buckets.buckets) == 1


def test_ingress_address_only_trusts_explicit_peer_and_single_address():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={
            "x-real-ip": "192.0.2.4",
            "x-forwarded-for": "192.0.2.5",
        },
    )
    assert auth_address(request, Settings()) == "127.0.0.1"
    settings = Settings(trusted_ingress_cidrs=["127.0.0.1/32"])
    assert auth_address(request, settings) == "192.0.2.4"
    request.headers["x-real-ip"] = "192.0.2.4, 192.0.2.5"
    assert auth_address(request, settings) == "127.0.0.1"


@pytest.mark.asyncio
async def test_chat_quota_is_user_scoped_across_sessions_and_proxy_peers(account):
    import time

    import httpx

    from app.core.security import digest
    from app.db.models import Conversation, Session, User, db
    from app.main import create_app

    first = account[0]
    second = User.create(email="second@example.com", password_hash="unused")
    Conversation.create(user=second)
    for raw, user in (("one", first), ("rotated", first), ("two", second)):
        Session.create(token_hash=digest(raw), user=user, expires=time.time() + 600)
    app = create_app(Settings(database_path=db.database, runner_enabled=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for n in range(30):
            response = await client.post(
                f"/api/chat/nonexistent-{n}",
                headers={"cookie": "cal_session=one"},
            )
            assert response.status_code == 404
        assert (
            await client.post(
                "/api/chat/messages",
                headers={
                    "cookie": "cal_session=rotated",
                    "x-forwarded-for": "192.0.2.1",
                },
            )
        ).status_code == 429
        assert (
            await client.post(
                "/api/chat/messages",
                json={
                    "content": "hello",
                    "client_request_id": str(uuid4()),
                },
                headers={"cookie": "cal_session=two"},
            )
        ).status_code == 200


def test_recovery_cli_inspects_and_resolves_without_network(account, tmp_path, monkeypatch, capsys):
    import app.maintenance as maintenance
    from app.db.models import db

    _, action_id = unknown(account)
    path = db.database
    # CLI normally runs in a fresh process, without the fixture's open connection.
    db.close()
    monkeypatch.setattr(maintenance, "Settings", lambda: SimpleNamespace(database_path=path))
    maintenance.main(["inspect-action", action_id])
    snapshot = json.loads(capsys.readouterr().out)
    observation = tmp_path / "evidence.json"
    observation.write_text('{"uid":"abc","status":"cancelled"}')
    maintenance.main(
        [
            "resolve-action",
            action_id,
            "--digest",
            snapshot["digest"],
            "--operator",
            "test-operator",
            "--note",
            "Verified audit evidence",
            "--outcome",
            "succeeded",
            "--observation-file",
            str(observation),
        ]
    )
    assert json.loads(capsys.readouterr().out)["evidence"]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_cancelled_database_unit_preserves_cancellation_if_worker_fails(account):
    started, release = threading.Event(), threading.Event()

    def fail_after_cancellation():
        started.set()
        assert release.wait(5)
        raise RuntimeError("worker failed")

    task = asyncio.create_task(query(fail_after_cancellation))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("status", ["running", "waiting_for_user"])
def test_legacy_unexposed_awaiting_confirmation_cannot_authorize(account, status):
    run_id, action_id, _ = proposal(account)
    Action.update(state="awaiting_confirmation").where(Action.id == action_id).execute()
    Run.update(status=status).where(Run.id == run_id).execute()
    accepted = accept(account[0], "yes", str(uuid4()))
    Run.update(status="running").where(Run.id == accepted["run_id"]).execute()
    assert Action.get_by_id(action_id).state == "superseded"
    assert not claim(action_id)


def test_claim_starting_after_terminal_commit_is_rejected(account):
    from concurrent.futures import ThreadPoolExecutor

    from app.db.models import db

    run_id, action_id, _ = proposal(account, False)
    started, release = threading.Event(), threading.Event()

    def delayed_claim():
        with db.connection_context():
            started.set()
            assert release.wait(5)
            return claim(action_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(delayed_claim)
        assert started.wait(5)
        finish(run_id, "failed", "run.failed", {})
        release.set()
        assert not pending.result()
    assert Action.get_by_id(action_id).state == "superseded"
