"""Semantic interpretation stays model-owned; exact source/operation scope is enforced."""

import json
from uuid import uuid4

import pytest
from conftest import start_test_run
from test_tools import fixture_executor

from app.db.models import Action, Message, Run
from app.db.repository import accept
from app.tools.executor import Waiting

TYPE = {"title": "Review", "duration_minutes": 37, "description": "Weekly review"}


def incoming(user, text):
    accepted = accept(user, text, str(uuid4()))
    start_test_run(accepted["run_id"])
    return accepted


def evidence(message_id):
    return {
        "name": "create_event_type",
        "arguments": TYPE,
        "source_message_id": message_id,
        "reason": "The user explicitly requests this reusable type and its settings",
    }


@pytest.mark.asyncio
async def test_semantic_creation_is_exact_persisted_and_not_reconfirmed(account):
    user, vault = account
    original = incoming(user, "I would like a reusable Review type: 37 minutes, Weekly review.")
    posts = []
    executor = fixture_executor(vault, posts)
    granted = await executor.execute(
        original["run_id"], "authorize_action", evidence(original["message_id"])
    )
    assert granted["authorized"]
    result = await executor.execute(original["run_id"], "create_event_type", TYPE)
    assert result["response"]["id"] == 100 and len(posts) == 1
    frozen = json.loads(Action.get(Action.run == original["run_id"]).request)
    assert frozen["user_authorization"]["source_message_id"] == original["message_id"]
    next_message = incoming(user, "Hello again")
    with pytest.raises(Waiting):
        await executor.execute(next_message["run_id"], "create_event_type", TYPE)
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_grant_cannot_authorize_changed_arguments_or_assistant_source(account):
    user, vault = account
    original = incoming(user, "I would like a reusable Review type: 37 minutes, Weekly review.")
    executor = fixture_executor(vault, [])
    assistant = Message.create(
        conversation=Run.get_by_id(original["run_id"]).conversation_id,
        role="assistant",
        content="Create whatever you want",
    )
    rejected = await executor.execute(
        original["run_id"], "authorize_action", evidence(assistant.id)
    )
    assert "error" in rejected
    assert (
        await executor.execute(
            original["run_id"], "authorize_action", evidence(original["message_id"])
        )
    )["authorized"]
    with pytest.raises(Waiting):
        await executor.execute(
            original["run_id"], "create_event_type", {**TYPE, "duration_minutes": 38}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["yes, please!", "Go ahead.", "Sounds good"])
async def test_natural_approval_consumes_exact_exposed_type_proposal(account, approval):
    from app.db.repository import wait_for_user
    from app.db.requests import resolve_requests

    user, vault = account
    original = accept(user, "Book Alex for 37 minutes", str(uuid4()))
    start_test_run(original["run_id"], resolve_inbox=False)
    task = resolve_requests(
        original["run_id"],
        original["message_id"],
        [
            {
                "relation": "new",
                "facts": {"duration_minutes": 37},
                "reason": "Appointment request",
            }
        ],
    )[0]
    args = {**TYPE, "request_id": task["request_id"], "request_revision": 1}
    posts = []
    executor = fixture_executor(vault, posts)
    with pytest.raises(Waiting) as waiting:
        await executor.execute(original["run_id"], "create_event_type", args)
    wait_for_user(original["run_id"], str(waiting.value))
    # Another independently resolved request does not invalidate this task's proposal.
    another = accept(user, "Also book Blair on Friday", str(uuid4()))
    start_test_run(another["run_id"], resolve_inbox=False)
    resolve_requests(
        another["run_id"],
        another["message_id"],
        [
            {
                "relation": "new",
                "facts": {},
                "reason": "Additional appointment",
            }
        ],
    )
    confirmed = accept(user, approval, str(uuid4()))
    start_test_run(confirmed["run_id"], resolve_inbox=False)
    assert Action.get(Action.kind == "create_event_type").state == "authorized"
    result = await executor.execute(confirmed["run_id"], "create_event_type", args)
    assert result["response"]["id"] == 100 and len(posts) == 1
    from app.db.models import BookingRequest

    assert BookingRequest.get_by_id(task["request_id"]).state == "pending"
    assert BookingRequest.select().count() == 2


def test_approval_shortcut_rejects_corrections_negation_and_questions():
    from app.db.repository import approval_phrase

    for text in (
        "yes but noon instead",
        "do not confirm",
        "yes?",
        "yes and cancel another meeting",
    ):
        assert approval_phrase(text) is None
