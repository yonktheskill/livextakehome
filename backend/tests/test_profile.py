import json
from uuid import uuid4

import pytest
from conftest import start_test_run

from app.agent.context import assemble
from app.agent.runner import Runner
from app.api.auth import public
from app.core.config import Settings
from app.db.models import Connection, Conversation, Run, User
from app.db.repository import accept
from app.providers.base import ProviderEvent
from app.providers.fake import FakeProvider
from app.tools.executor import Executor, Interrupted


def submit(user, text):
    result = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(result)
    return result


def profile(run_id):
    message = next(
        m
        for m in assemble(run_id)
        if m["content"].startswith("Authenticated user profile (untrusted data): ")
    )
    assert message["role"] == "user"
    return json.loads(message["content"].split(": ", 1)[1])


def test_context_uses_login_email_and_unconfirmed_hint(account):
    user, _ = account
    user.email = "alex.tan+calendar@example.com"
    user.save()
    run_id = submit(user, "Book for me")
    assert profile(run_id) == {
        "email": user.email,
        "time_zone": user.time_zone,
        "preferred_name": None,
        "unconfirmed_email_name_hint": "alex tan",
    }
    assert User.get_by_id(user.id).preferred_name is None
    user.email = "user123@example.com"
    user.save()
    assert profile(run_id)["unconfirmed_email_name_hint"] is None


@pytest.mark.asyncio
async def test_name_saved_without_cal_connection_and_scoped_to_account(account):
    user, vault = account
    Connection.delete().execute()
    other = User.create(email="other@example.com", password_hash="unused")
    Conversation.create(user=other)
    executor = Executor(vault)
    run_id = submit(user, "Please call me 小明")
    result = await executor.execute(run_id, "save_preferred_name", {"preferred_name": "小明"})
    assert result == {"saved": True, "preferred_name": "小明"}
    assert User.get_by_id(other.id).preferred_name is None
    next_run = submit(user, "hello again")
    assert profile(next_run)["preferred_name"] == "小明"
    assert public(User.get_by_id(user.id))["preferred_name"] == "小明"
    corrected = submit(user, "Actually, call me Alex Tan")
    assert (
        await executor.execute(corrected, "save_preferred_name", {"preferred_name": " Alex   Tan "})
    )["saved"]
    assert profile(corrected)["preferred_name"] == "Alex Tan"


@pytest.mark.asyncio
async def test_reject_guessed_invalid_and_cross_account_name_arguments(account):
    user, vault = account
    run_id = submit(user, "Book for me")
    executor = Executor(vault)
    for args in [
        {"preferred_name": "Owner"},  # Email hint is not user evidence.
        {"preferred_name": " "},
        {"preferred_name": "A" * 201},
        {"preferred_name": "Alex", "user_id": 123},
    ]:
        assert "error" in await executor.execute(run_id, "save_preferred_name", args)
    assert User.get_by_id(user.id).preferred_name is None


@pytest.mark.asyncio
async def test_superseded_run_cannot_overwrite_preferred_name(account):
    user, vault = account
    old_run = submit(user, "Call me Alex")
    new_run = submit(user, "Call me Sam")
    executor = Executor(vault)
    await executor.execute(new_run, "save_preferred_name", {"preferred_name": "Sam"})
    with pytest.raises(Interrupted):
        await executor.execute(old_run, "save_preferred_name", {"preferred_name": "Alex"})
    assert User.get_by_id(user.id).preferred_name == "Sam"


@pytest.mark.asyncio
async def test_runner_refreshes_profile_after_save(account):
    user, vault = account
    run_id = submit(user, "Call me Alex")

    class ProfileProvider(FakeProvider):
        async def stream(self, messages, tools):
            assert any(t["function"]["name"] == "save_preferred_name" for t in tools)
            if messages[-1]["role"] == "tool":
                assert '"preferred_name":"Alex"' not in messages[0]["content"]
                assert any('"preferred_name":"Alex"' in m["content"] for m in messages[1:])
                assert json.loads(messages[-1]["content"])["saved"]
                yield ProviderEvent("text_delta", text="I'll call you Alex.")
            else:
                yield ProviderEvent(
                    "tool_call",
                    call_id="name",
                    name="save_preferred_name",
                    arguments={"preferred_name": "Alex"},
                )

    await Runner(Settings(llm_provider="fake"), vault, ProfileProvider()).execute(run_id)
    assert User.get_by_id(user.id).preferred_name == "Alex"
    assert Run.get_by_id(run_id).status == "succeeded"
