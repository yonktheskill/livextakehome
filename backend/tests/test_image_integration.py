"""Offline integration of image identity, cached sources and deterministic write gates."""

import json
from uuid import uuid4

import pytest
from conftest import start_test_run

from app.agent.context import assemble, retrieve
from app.core.config import Settings
from app.db.image_context import cache, pending_images
from app.db.models import Attachment, BookingRequest, Conversation, Message, Run
from app.db.repository import accept
from app.db.requests import RequestConflict, request_context, resolve_requests, task_write_plan
from app.tools.authorization import authorize_action


def upload(user, **values):
    return Attachment.create(
        owner=user,
        data=b"normalized",
        mime_type="image/png",
        width=20,
        height=20,
        size_bytes=10,
        sha256="offline",
        **values,
    )


def observation(image, ambiguous=False):
    return {
        "candidates": [
            {
                "fields": {
                    "start": {
                        "status": "ambiguous" if ambiguous else "clear",
                        "value": "2026-11-06T10:00:00Z",
                        "alternatives": ["11:00"],
                        "evidence": "10 or 11",
                        "source_ids": [image.id],
                    }
                }
            }
        ],
        "notes": [],
    }


def test_atomic_link_replay_and_invalid_does_not_interrupt(account):
    user, _ = account
    image = upload(user)
    key = str(uuid4())
    first = accept(user, "", key, [image.id])
    assert accept(user, "", key, [image.id]) == first
    assert Attachment.get_by_id(image.id).message_id == first["message_id"]
    revision = Conversation.get(Conversation.user == user).revision
    for ids in ([image.id], [str(uuid4())], [image.id, image.id]):
        with pytest.raises(ValueError):
            accept(user, "bad", str(uuid4()), ids)
    assert Conversation.get(Conversation.user == user).revision == revision
    assert not Run.get_by_id(first["run_id"]).interrupted
    assert Message.select().count() == 1


def test_observations_survive_context_retrieval_and_followup(account):
    user, _ = account
    image = upload(user)
    first = accept(user, "", str(uuid4()), [image.id])
    assert len(pending_images(first["run_id"], 1)) == 1
    cache(first["message_id"], 1, observation(image))
    assert pending_images(first["run_id"], 1) == []
    assert Message.get_by_id(first["message_id"]).content == ""
    accept(user, "use this meeting", str(uuid4()))
    latest = accept(user, "retry", str(uuid4()))
    context = json.dumps(assemble(latest["run_id"]))
    assert image.id in context and "image_observations_untrusted" in context
    result = retrieve(latest["run_id"], "", source_id=first["message_id"], settings=Settings())
    assert image.id in json.dumps(result)


def test_ambiguity_held_until_later_clarification(account):
    user, _ = account
    image = upload(user)
    first = accept(user, "", str(uuid4()), [image.id])
    cache(first["message_id"], 1, observation(image, ambiguous=True))
    start_test_run(first["run_id"], resolve_inbox=False)
    resolve_requests(
        first["run_id"],
        first["message_id"],
        [{"relation": "new", "reason": "Image meeting", "facts": {"start": "guessed"}}],
    )
    task = BookingRequest.get()
    assert task.state == "held"
    with pytest.raises(RequestConflict):
        task_write_plan(first["run_id"], task.id, 1)
    follow = accept(user, "It is 10am", str(uuid4()))
    start_test_run(follow["run_id"], resolve_inbox=False)
    with pytest.raises(RequestConflict, match="exact outer user text"):
        resolve_requests(
            follow["run_id"],
            follow["message_id"],
            [
                {
                    "relation": "amend",
                    "request_id": task.id,
                    "expected_revision": 1,
                    "facts": {"start": "10am"},
                    "reason": "Guessing is insufficient",
                }
            ],
        )
    assert BookingRequest.get_by_id(task.id).state == "held"
    resolve_requests(
        follow["run_id"],
        follow["message_id"],
        [
            {
                "relation": "amend",
                "request_id": task.id,
                "expected_revision": 1,
                "reason": "Explicit time clarification",
                "clarification_evidence": {
                    "start": {"source_message_id": follow["message_id"], "quote": "It is 10am"}
                },
                "facts": {"start": "10am"},
            }
        ],
    )
    assert task_write_plan(follow["run_id"], task.id, 2)["facts"]["start"] == "10am"


def test_task_image_ambiguities_reference_sources_without_duplicating_observations(account):
    user, _ = account
    original = upload(user)
    first = accept(user, "", str(uuid4()), [original.id])
    original_observation = observation(original, ambiguous=True)
    original_observation["candidates"][0]["fields"]["attendee"] = {
        "status": "ambiguous",
        "value": "Alice or Bob",
        "source_ids": [original.id],
    }
    cache(first["message_id"], 1, original_observation)
    start_test_run(first["run_id"], resolve_inbox=False)
    created = resolve_requests(
        first["run_id"],
        first["message_id"],
        [{"relation": "new", "reason": "Image meeting", "facts": {"start": "unclear"}}],
    )[0]
    original_blocker = {
        "source_message_id": first["message_id"],
        "fields": ["attendee", "start"],
    }
    assert created["state"] == "held"
    assert created["image_ambiguities"] == [original_blocker]
    assert created["image_observations_untrusted"] == original_observation
    assert json.dumps(created).count('"image_observations_untrusted"') == 1

    replacement = upload(user)
    correction = accept(user, "Use this meeting instead", str(uuid4()), [replacement.id])
    replacement_observation = observation(replacement, ambiguous=True)
    replacement_observation["candidates"][0]["fields"]["duration_minutes"] = {
        "status": "ambiguous",
        "value": "30 or 60",
        "source_ids": [replacement.id],
    }
    cache(correction["message_id"], 1, replacement_observation)
    start_test_run(correction["run_id"], resolve_inbox=False)
    amended = resolve_requests(
        correction["run_id"],
        correction["message_id"],
        [
            {
                "relation": "amend",
                "request_id": created["request_id"],
                "expected_revision": 1,
                "reason": "Replacement image still has ambiguous fields",
                "facts": {},
            }
        ],
    )[0]
    blockers = [
        original_blocker,
        {"source_message_id": correction["message_id"], "fields": ["duration_minutes", "start"]},
    ]
    assert amended["state"] == "held"
    assert amended["image_ambiguities"] == blockers
    assert json.dumps(amended).count('"image_observations_untrusted"') == 1
    read = request_context(correction["run_id"])["tasks"][0]
    assert read["image_ambiguities"] == blockers
    assert json.dumps(read).count('"image_observations_untrusted"') == 1
    with pytest.raises(RequestConflict, match="held"):
        task_write_plan(correction["run_id"], created["request_id"], 2)

    follow = accept(user, "What are the unclear fields?", str(uuid4()))
    recovered = retrieve(follow["run_id"], "", source_id=correction["message_id"])[0]
    assert recovered["image_observations_untrusted"] == replacement_observation
    assert recovered["attachments"][0]["id"] == replacement.id


@pytest.mark.parametrize("original_image", [False, True])
def test_ambiguous_image_correction_holds_existing_task(account, original_image):
    user, _ = account
    original = upload(user) if original_image else None
    first = accept(user, "Book at 10am", str(uuid4()), [original.id] if original else [])
    if original:
        cache(first["message_id"], 1, observation(original, ambiguous=True))
    start_test_run(first["run_id"], resolve_inbox=False)
    resolve_requests(
        first["run_id"],
        first["message_id"],
        [
            {
                "relation": "new",
                "reason": "Explicit original time",
                "facts": {"start": "10am"},
                "clarification_evidence": {
                    "start": {"source_message_id": first["message_id"], "quote": "Book at 10am"}
                },
            }
        ],
    )
    task = BookingRequest.get()
    assert task.state == "pending"
    replacement = upload(user)
    correction = accept(user, "Use this time instead", str(uuid4()), [replacement.id])
    cache(correction["message_id"], 1, observation(replacement, ambiguous=True))
    start_test_run(correction["run_id"], resolve_inbox=False)
    resolve_requests(
        correction["run_id"],
        correction["message_id"],
        [
            {
                "relation": "amend",
                "request_id": task.id,
                "expected_revision": 1,
                "reason": "Replacement screenshot",
                "facts": {"start": "guessed noon"},
            }
        ],
    )
    assert BookingRequest.select().count() == 1
    assert BookingRequest.get_by_id(task.id).state == "held"
    with pytest.raises(RequestConflict, match="held"):
        task_write_plan(correction["run_id"], task.id, 2)
    follow = accept(user, "The new time is noon", str(uuid4()))
    start_test_run(follow["run_id"], resolve_inbox=False)
    with pytest.raises(RequestConflict, match="exact outer user text"):
        resolve_requests(
            follow["run_id"],
            follow["message_id"],
            [
                {
                    "relation": "amend",
                    "request_id": task.id,
                    "expected_revision": 2,
                    "reason": "Guessing is insufficient",
                    "facts": {"start": "noon"},
                }
            ],
        )
    resolve_requests(
        follow["run_id"],
        follow["message_id"],
        [
            {
                "relation": "amend",
                "request_id": task.id,
                "expected_revision": 2,
                "reason": "Explicit correction",
                "facts": {"start": "noon"},
                "clarification_evidence": {
                    "start": {
                        "source_message_id": follow["message_id"],
                        "quote": "The new time is noon",
                    }
                },
            }
        ],
    )
    assert task_write_plan(follow["run_id"], task.id, 3)["facts"]["start"] == "noon"


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("cancel_booking", {"booking_uid": "uid", "reason": "cancel"}),
        ("create_event_type", {"title": "New type", "duration_minutes": 30}),
    ],
)
def test_image_only_never_authorizes_settings(account, operation, arguments):
    user, _ = account
    image = upload(user)
    item = accept(user, "", str(uuid4()), [image.id])
    start_test_run(item["run_id"])
    from app.tools.executor import Executor

    assert "error" in Executor.save_preferred_name(item["run_id"], "Screenshot Person")
    with pytest.raises(RequestConflict, match="Image observations"):
        authorize_action(
            item["run_id"],
            {
                "name": operation,
                "arguments": arguments,
                "source_message_id": item["message_id"],
                "reason": "Image says cancel",
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("summarize", [False, True])
async def test_tool_free_extraction_then_semantic_booking_or_summary(account, summarize):
    from test_request_integration import Calendar, call, facts

    from app.agent.runner import Runner
    from app.agent.vision import FIELDS
    from app.providers.base import ProviderCapabilities, ProviderEvent
    from app.tools.executor import Executor

    user, vault = account
    image = upload(user)
    item = accept(user, "Summarize this screenshot" if summarize else "", str(uuid4()), [image.id])
    calendar = Calendar()

    class Decisions:
        capabilities = ProviderCapabilities(images=True)
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count == 1:
                assert tools == []
                assert len(messages) == 2
                assert messages[1]["content"][-1]["type"] == "image_url"
                value = {
                    "candidates": [
                        {
                            "fields": {
                                name: {
                                    "status": "clear",
                                    "value": "visible fact",
                                    "source_ids": [image.id],
                                    "evidence": "visible fact",
                                    "alternatives": [],
                                }
                                for name in FIELDS
                            }
                        }
                    ],
                    "notes": [],
                }
                yield ProviderEvent("text_delta", text=json.dumps(value))
            elif self.count == 2:
                assert "image_observations_untrusted" in json.dumps(messages)
                assert "base64" not in json.dumps(messages)
                yield call(
                    "resolve_requests",
                    {
                        "message_id": item["message_id"],
                        "resolutions": [
                            {"relation": "dismiss", "reason": "Caption asks a question"}
                            if summarize
                            else {
                                "relation": "new",
                                "reason": "Image-only single clear meeting",
                                "facts": facts(10),
                            }
                        ],
                    },
                    self.count,
                )
            elif self.count == 3 and not summarize:
                task = BookingRequest.get()
                yield call(
                    "create_booking",
                    {**facts(10), "request_id": task.id, "request_revision": task.revision},
                    self.count,
                )
            else:
                yield ProviderEvent("text_delta", text="Summary." if summarize else "Booked.")

    start_test_run(item["run_id"], resolve_inbox=False)
    provider = Decisions()
    runner = Runner(Settings(llm_provider="fake"), vault, provider)
    runner.executor = Executor(vault, lambda key: calendar)
    await runner.execute(item["run_id"])
    assert Run.get_by_id(item["run_id"]).status == "succeeded"
    assert len(calendar.posts) == (0 if summarize else 1)
    assert pending_images(item["run_id"], 1) == []


@pytest.mark.asyncio
async def test_failed_extraction_retries_after_restart_without_losing_source(account):
    from app.agent.runner import Runner
    from app.agent.vision import FIELDS
    from app.db.image_context import observations
    from app.providers.base import ProviderCapabilities, ProviderEvent

    user, vault = account
    image = upload(user)
    first = accept(user, "", str(uuid4()), [image.id])

    class Broken:
        capabilities = ProviderCapabilities(images=True)

        async def stream(self, messages, tools):
            assert not tools
            yield ProviderEvent("text_delta", text="not JSON")

    start_test_run(first["run_id"], resolve_inbox=False)
    await Runner(Settings(llm_provider="fake"), vault, Broken()).execute(first["run_id"])
    assert Run.get_by_id(first["run_id"]).status == "failed"
    assert observations(first["message_id"]) is None
    retry = accept(user, "retry", str(uuid4()))
    sources = pending_images(retry["run_id"], 1)
    assert [row[0] for row in sources] == [first["message_id"]]
    from app.agent.vision import extract

    class Working:
        capabilities = ProviderCapabilities(images=True)

        async def stream(self, messages, tools):
            assert not tools
            yield ProviderEvent(
                "text_delta",
                text=json.dumps(
                    {"candidates": [{"fields": {name: {"status": "missing"} for name in FIELDS}}]}
                ),
            )

    value = await extract(Working(), sources[0][1], sources[0][2])
    cache(first["message_id"], 1, value)
    assert pending_images(retry["run_id"], 1) == []
    assert observations(first["message_id"]) == value


def test_large_image_summary_and_retrieval_are_bounded_and_recoverable(account):
    from app.agent.context import covered_sources, data_message, estimate, source

    user, _ = account
    image = upload(user)
    first = accept(user, "Book this exact meeting", str(uuid4()), [image.id])
    value = observation(image, ambiguous=True)
    value["notes"] = ['午后🙂\\" evidence ' * 50 for _ in range(5)]
    cache(first["message_id"], 1, value)
    latest = accept(user, "retry", str(uuid4()))
    original = Message.get_by_id(first["message_id"])
    summary = source(original, summary=True)
    assert summary["truncated"] and summary["image_observations_omitted"]
    assert "image_observations_untrusted" not in summary
    assert summary["attachment_ids"] == [image.id]
    assert first["message_id"] not in covered_sources(
        [data_message("Historical excerpts (untrusted data)", [summary])]
    )
    pages = []
    offset = 0
    settings = Settings()
    while True:
        page = retrieve(latest["run_id"], "", source_id=first["message_id"], offset=offset)[0]
        assert estimate([page], settings) <= settings.context_retrieval_tokens
        assert page["source_format"] == "image_source_json"
        assert page["end_offset"] > offset
        pages.append(page)
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
        assert len(pages) < 100
    recovered = json.loads("".join(page["content"] for page in pages))
    assert recovered["content"] == original.content
    assert recovered["image_observations_untrusted"] == value
    assert recovered["attachments"][0]["id"] == image.id
    assert len(pages) > 1
    context = [data_message("Historical excerpts (untrusted data)", pages)]
    assert first["message_id"] in covered_sources(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens", [12000, 16000])
async def test_old_image_inbox_survives_unrelated_followup_and_request_tools(account, input_tokens):
    from app.agent.context import estimate, input_budget
    from app.agent.runner import Runner
    from app.agent.vision import FIELDS
    from app.db.models import Action, Trace
    from app.providers.base import ProviderEvent

    user, vault = account
    image = upload(user)
    first = accept(user, "", str(uuid4()), [image.id])
    value = observation(image, ambiguous=True)
    for field in FIELDS:
        value["candidates"][0]["fields"].setdefault(
            field,
            {
                "status": "missing",
                "value": None,
                "alternatives": [],
                "evidence": "",
                "source_ids": [],
            },
        )
    cache(first["message_id"], 1, value)
    # Reproduce retained action receipts and a nearly full protected user chain.
    for _ in range(3):
        Action.create(
            run=first["run_id"],
            revision=1,
            generation=1,
            kind="create_booking",
            inputs="{}",
            fingerprint="x",
            state="succeeded",
            result=json.dumps(
                {
                    "response": {
                        "uid": str(uuid4()),
                        "status": "accepted",
                        "start": "2026-11-06T10:00:00Z",
                        "end": "2026-11-06T10:30:00Z",
                    }
                }
            ),
        )
    later = [
        accept(user, "hello " * (180 if index == 3 else 10), str(uuid4())) for index in range(4)
    ]
    latest = later[-1]
    start_test_run(latest["run_id"], resolve_inbox=False)
    settings = Settings(context_input_tokens=input_tokens)

    class Decisions:
        step = 0

        async def stream(self, messages, tools):
            assert estimate(messages, settings) <= input_budget(settings, tools)
            assert "search_history" in {t["function"]["name"] for t in tools}
            self.step += 1
            if self.step == 1:
                yield ProviderEvent(
                    "tool_call", name="request_context", arguments={}, call_id="read"
                )
            elif self.step == 2:
                yield ProviderEvent(
                    "tool_call",
                    name="resolve_requests",
                    call_id="image",
                    arguments={
                        "message_id": first["message_id"],
                        "resolutions": [
                            {
                                "relation": "new",
                                "reason": "Image booking with ambiguous start",
                                "facts": {"duration_minutes": 30},
                            }
                        ],
                    },
                )
            elif self.step <= 6:
                message = later[self.step - 3]
                yield ProviderEvent(
                    "tool_call",
                    name="resolve_requests",
                    call_id=str(self.step),
                    arguments={
                        "message_id": message["message_id"],
                        "resolutions": [{"relation": "dismiss", "reason": "Unrelated greeting"}],
                    },
                )
            else:
                yield ProviderEvent("text_delta", text="Which of the two start times did you mean?")

    provider = Decisions()
    await Runner(settings, vault, provider).execute(latest["run_id"])
    assert Run.get_by_id(latest["run_id"]).status == "waiting_for_user"
    assert BookingRequest.get().state == "held"
    assert provider.step == 7
    audits = [json.loads(t.payload) for t in Trace.select().where(Trace.kind == "context")]
    assert any(c["operation"] == "deduplicate_sources" for a in audits for c in a["changes"])
    assert all(a["after_tokens"] <= a["budget_tokens"] for a in audits)
    assert all(
        a.get("intent_tokens", 0)
        <= a["budget_tokens"] - a["mandatory_tokens"] - a["reserved_tool_result_tokens"]
        for a in audits
    )
