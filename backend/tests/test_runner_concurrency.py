"""Real database/runner tests with event gates instead of provider timing assumptions."""

import asyncio
import json
import threading
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.agent import runner as runner_module
from app.agent.runner import Runner
from app.core.config import Settings
from app.db.database import query
from app.db.models import Action, Conversation, Event, Message, Run, User, db
from app.db.repository import accept, claim_run, finish
from app.main import create_app
from app.providers.base import ProviderEvent
from app.tools.executor import Waiting


def enqueue(label, user=None):
    if user is None:
        user = User.create(email=f"{uuid4()}@example.com", password_hash="unused")
        Conversation.create(user=user)
    return accept(user, label, str(uuid4()))["run_id"]


async def wait_status(run_id, status):
    async with asyncio.timeout(5):
        while await query(lambda: Run.get_by_id(run_id).status) != status:
            await asyncio.sleep(0.01)


class GatedProvider:
    def __init__(self, *labels):
        self.started = {label: asyncio.Event() for label in labels}
        self.release = {label: asyncio.Event() for label in labels}
        self.active = 0
        self.peak = 0
        self.closed = False

    async def stream(self, messages, tools):
        label = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))[
            "content"
        ]
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started[label].set()
        try:
            await self.release[label].wait()
            yield ProviderEvent("text_delta", text=f"Reply to {label}")
        finally:
            self.active -= 1

    async def wait_started(self, *labels):
        async with asyncio.timeout(5):
            await asyncio.gather(*(self.started[label].wait() for label in labels))

    async def aclose(self):
        assert self.active == 0
        self.closed = True


@pytest.mark.asyncio
async def test_bounded_pool_services_other_users_while_a_run_waits(account):
    _, vault = account
    runs = {label: enqueue(label) for label in ("first", "second", "third")}
    provider = GatedProvider(*runs)
    runner = Runner(Settings(max_concurrent_runs=2), vault, provider)
    supervisor = asyncio.create_task(runner.supervise())
    try:
        await provider.wait_started("first", "second")
        assert Run.get_by_id(runs["third"]).status == "queued"
        assert provider.active == 2
        provider.release["first"].set()
        await provider.wait_started("third")
        assert not provider.release["second"].is_set()
        assert provider.active == provider.peak == 2
        provider.release["second"].set()
        provider.release["third"].set()
        for label, run_id in runs.items():
            await wait_status(run_id, "succeeded")
            assert (
                Message.get(client_request_id=f"assistant:{run_id}").content == f"Reply to {label}"
            )
            events = list(Event.select().where(Event.run == run_id).order_by(Event.sequence))
            assert [event.sequence for event in events] == [1, 2]
            assert [event.kind for event in events] == [
                "message.delta",
                "run.completed",
            ]
    finally:
        runner.stopping.set()
        await asyncio.wait_for(supervisor, 5)
    assert provider.active == 0


@pytest.mark.asyncio
async def test_correction_waits_for_its_conversation_without_blocking_other_users(
    account,
):
    user, vault = account
    first = enqueue("first", user)
    provider = GatedProvider("first", "correction", "other")
    runner = Runner(Settings(max_concurrent_runs=2), vault, provider)
    supervisor = asyncio.create_task(runner.supervise())
    try:
        await provider.wait_started("first")
        correction = enqueue("correction", user)
        other = enqueue("other")
        await provider.wait_started("other")
        assert Run.get_by_id(correction).status == "queued"
        assert not provider.started["correction"].is_set()
        provider.release["first"].set()
        await provider.wait_started("correction")
        assert Run.get_by_id(first).status == "interrupted"
        assert (
            not Message.select().where(Message.client_request_id == f"assistant:{first}").exists()
        )
        provider.release["correction"].set()
        provider.release["other"].set()
        await wait_status(correction, "succeeded")
        await wait_status(other, "succeeded")
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_run_keeps_workers_available(account):
    _, vault = account
    failed = enqueue("failure")
    tool_started, fail_tool = asyncio.Event(), asyncio.Event()

    class Provider(GatedProvider):
        async def stream(self, messages, tools):
            if json.loads(messages[-1]["content"])["content"] == "failure":
                yield ProviderEvent(
                    "tool_call", name="list_event_types", arguments={}, call_id="one"
                )
            else:
                async for event in super().stream(messages, tools):
                    yield event

    class FailingExecutor:
        async def execute(self, run_id, name, arguments):
            tool_started.set()
            await fail_tool.wait()
            raise RuntimeError("private upstream details")

    provider = Provider("waiting", "later")
    runner = Runner(Settings(max_concurrent_runs=2), vault, provider)
    runner.executor = FailingExecutor()
    supervisor = asyncio.create_task(runner.supervise())
    try:
        await asyncio.wait_for(tool_started.wait(), 5)
        waiting = enqueue("waiting")
        await provider.wait_started("waiting")
        fail_tool.set()
        await wait_status(failed, "failed")
        assert Run.get_by_id(waiting).status == "running"
        later = enqueue("later")
        await provider.wait_started("later")
        provider.release["later"].set()
        await wait_status(later, "succeeded")
        provider.release["waiting"].set()
        await wait_status(waiting, "succeeded")
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


@pytest.mark.asyncio
async def test_lifespan_drains_all_workers_and_preserves_uncertain_writes(account):
    first, second, queued = [enqueue(label) for label in ("first", "second", "queued")]
    provider = GatedProvider("first", "second", "queued")
    app = create_app(
        Settings(
            database_path=db.database,
            master_secret=Fernet.generate_key().decode(),
            max_concurrent_runs=2,
        ),
        provider,
    )
    async with app.router.lifespan_context(app):
        await provider.wait_started("first", "second")
        action = Action.create(
            run=first,
            revision=1,
            generation=1,
            kind="create_booking",
            inputs="{}",
            fingerprint="digest",
            state="dispatching",
        )
    assert provider.closed
    assert Action.get_by_id(action.id).state == "unknown"
    assert Run.get_by_id(first).status == "needs_reconciliation"
    assert Run.get_by_id(second).status == "failed"
    assert Run.get_by_id(queued).status == "queued"
    assert not any(t.get_name().startswith("agent-worker-") for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_shutdown_during_offloaded_claim_settles_the_claim(account, monkeypatch):
    _, vault = account
    run_id = enqueue("first")
    claimed, release = threading.Event(), threading.Event()

    def delayed_claim():
        result = claim_run()
        claimed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(runner_module, "claim_run", delayed_claim)
    provider = GatedProvider("first")
    runner = Runner(Settings(max_concurrent_runs=1), vault, provider)
    supervisor = asyncio.create_task(runner.supervise())
    try:
        assert await asyncio.to_thread(claimed.wait, 5)
        supervisor.cancel()
        # Keep the transaction thread blocked until cancellation reaches its consumer.
        async with asyncio.timeout(5):
            while not any(
                t.get_name().startswith("agent-worker-") and t.cancelling()
                for t in asyncio.all_tasks()
            ):
                await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.gather(supervisor, return_exceptions=True)
    assert Run.get_by_id(run_id).status == "interrupted"
    assert not provider.started["first"].is_set()
    assert Event.get(Event.run == run_id).kind == "run.interrupted"


@pytest.mark.asyncio
async def test_cancellation_during_exception_bookkeeping_settles_run(account, monkeypatch):
    _, vault = account
    run_id = enqueue("confirmation")
    assert claim_run() == run_id
    finishing = asyncio.Event()

    async def delayed_finish(fn, *args, **kwargs):
        if fn is finish and args[1] == "waiting_for_user":
            finishing.set()
            await asyncio.Event().wait()
        return await query(fn, *args, **kwargs)

    class ConfirmingRunner(Runner):
        async def loop(self, run_id):
            raise Waiting("Confirm cancellation.")

    monkeypatch.setattr(runner_module, "query", delayed_finish)
    runner = ConfirmingRunner(Settings(), vault, GatedProvider())
    task = asyncio.create_task(runner.execute(run_id))
    try:
        await asyncio.wait_for(finishing.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert Run.get_by_id(run_id).status == "failed"
    assert Event.get(Event.run == run_id, kind="run.failed")


@pytest.mark.asyncio
async def test_cancellation_preserves_a_terminal_commit(account, monkeypatch):
    _, vault = account
    run_id = enqueue("first")
    assert claim_run() == run_id
    committed = asyncio.Event()

    async def delayed_result(fn, *args, **kwargs):
        result = await query(fn, *args, **kwargs)
        if fn is finish and args[1] == "succeeded":
            committed.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(runner_module, "query", delayed_result)
    provider = GatedProvider("first")
    provider.release["first"].set()
    runner = Runner(Settings(), vault, provider)
    task = asyncio.create_task(runner.execute(run_id))
    try:
        await asyncio.wait_for(committed.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert Run.get_by_id(run_id).status == "succeeded"
    assert Event.select().where(Event.run == run_id, Event.kind == "run.completed").count() == 1
    assert not Event.select().where(Event.run == run_id, Event.kind == "run.failed").exists()
