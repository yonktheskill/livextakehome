"""Runner, real executor and dispatch integration; scripted semantics and offline API."""

import json
from uuid import uuid4

import pytest
from conftest import start_test_run

from app.agent.runner import Runner
from app.core.config import Settings
from app.db.models import BookingRequest, Run
from app.db.repository import accept
from app.providers.base import ProviderEvent
from app.tools.executor import Executor


def submit(user, text):
    return accept(user, text, str(uuid4()))


def call(name, arguments, count):
    return ProviderEvent("tool_call", name=name, arguments=arguments, call_id=str(count))


def facts(hour, name="Alex"):
    return {
        "start": f"2026-11-06T{hour:02}:00:00Z",
        "duration_minutes": 30,
        "event_type_id": 10,
        "attendee": {"name": name, "email": f"{name.lower()}@example.com", "time_zone": "UTC"},
    }


class Calendar:
    def __init__(self):
        self.posts, self.bookings = [], {}

    async def request(self, method, path, *, params=None, payload=None, **kwargs):
        if method == "POST":
            self.posts.append((path, payload))
            uid, start = f"uid_{len(self.posts)}", payload["start"]
            row = (
                {"attendees": [payload["attendee"]]}
                if path == "/v2/bookings"
                else self.bookings[path.split("/")[-2]].copy()
            )
            row.update(
                uid=uid,
                start=start,
                end=start.replace(":00:00", ":30:00"),
                eventTypeId=10,
                hosts=[{"id": 42}],
                status="accepted",
            )
            self.bookings[uid] = row
            return row
        if path == "/v2/event-types/10":
            return {"id": 10, "ownerId": 42, "lengthInMinutes": 30, "locations": []}
        if path == "/v2/slots":
            return {"day": [{"start": params["start"]}]}
        if path.startswith("/v2/bookings/"):
            return self.bookings[path.rsplit("/", 1)[1]]
        raise AssertionError((method, path))

    async def close(self):
        pass


async def execute(accepted, vault, calendar, provider):
    start_test_run(accepted["run_id"], resolve_inbox=False)
    runner = Runner(Settings(llm_provider="fake"), vault, provider)
    runner.executor = Executor(vault, lambda key: calendar)
    await runner.execute(accepted["run_id"])
    assert Run.get_by_id(accepted["run_id"]).status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [False, True])
async def test_rapid_requests_are_added_or_amended_before_booking(account, correction):
    user, vault = account
    first = submit(user, "Book Alex tomorrow at 10 for 30 minutes")
    second = submit(
        user,
        "Let's book this for 12pm instead"
        if correction
        else "Also book Blair tomorrow at 11 for 30 minutes",
    )
    calendar = Calendar()

    class Decisions:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count == 1:
                args = {
                    "message_id": first["message_id"],
                    "resolutions": [
                        {"relation": "new", "facts": facts(10), "reason": "First booking"}
                    ],
                }
                yield call("resolve_requests", args, self.count)
            elif self.count == 2:
                original = BookingRequest.select().get()
                resolution = (
                    {
                        "relation": "amend",
                        "request_id": original.id,
                        "expected_revision": 1,
                        "facts": {"start": facts(12)["start"]},
                        "reason": "This and instead refer to the original booking",
                    }
                    if correction
                    else {
                        "relation": "new",
                        "facts": facts(11, "Blair"),
                        "reason": "Explicit additional booking",
                    }
                )
                yield call(
                    "resolve_requests",
                    {"message_id": second["message_id"], "resolutions": [resolution]},
                    self.count,
                )
            else:
                pending = BookingRequest.select().where(BookingRequest.state == "pending").first()
                if pending:
                    yield call(
                        "create_booking",
                        {
                            **json.loads(pending.facts),
                            "request_id": pending.id,
                            "request_revision": pending.revision,
                        },
                        self.count,
                    )
                else:
                    yield ProviderEvent("text_delta", text="The requested bookings are complete.")

    await execute(second, vault, calendar, Decisions())
    assert len(calendar.posts) == (1 if correction else 2)
    assert {payload["start"] for _, payload in calendar.posts} == (
        {facts(12)["start"]} if correction else {facts(10)["start"], facts(11)["start"]}
    )
    assert all(task.state == "succeeded" for task in BookingRequest.select())


@pytest.mark.asyncio
async def test_successful_booking_followup_reschedules_actual_uid(account):
    user, vault = account
    first = submit(user, "Book Alex tomorrow at 10 for 30 minutes")
    calendar = Calendar()

    class Initial:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count == 1:
                yield call(
                    "resolve_requests",
                    {
                        "message_id": first["message_id"],
                        "resolutions": [
                            {"relation": "new", "facts": facts(10), "reason": "Explicit booking"}
                        ],
                    },
                    self.count,
                )
            elif self.count == 2:
                task = BookingRequest.select().get()
                yield call(
                    "create_booking",
                    {**facts(10), "request_id": task.id, "request_revision": 1},
                    self.count,
                )
            else:
                yield ProviderEvent("text_delta", text="Booked.")

    await execute(first, vault, calendar, Initial())
    task = BookingRequest.select().get()
    assert task.booking_uid == "uid_1"
    correction = submit(user, "Let's book this for 12pm instead")

    class Correction:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count == 1:
                yield call(
                    "resolve_requests",
                    {
                        "message_id": correction["message_id"],
                        "resolutions": [
                            {
                                "relation": "amend",
                                "request_id": task.id,
                                "expected_revision": 1,
                                "facts": {"start": facts(12)["start"]},
                                "reason": "Move completed booking to noon",
                            }
                        ],
                    },
                    self.count,
                )
            elif self.count == 2:
                yield call(
                    "reschedule_booking",
                    {
                        "booking_uid": "uid_1",
                        "new_start": facts(12)["start"],
                        "request_id": task.id,
                        "request_revision": 2,
                    },
                    self.count,
                )
            else:
                yield ProviderEvent("text_delta", text="Moved the booking to noon.")

    await execute(correction, vault, calendar, Correction())
    assert [path for path, _ in calendar.posts] == ["/v2/bookings", "/v2/bookings/uid_1/reschedule"]
    refreshed = BookingRequest.get_by_id(task.id)
    assert refreshed.booking_uid == "uid_2" and refreshed.state == "succeeded"
    assert json.loads(refreshed.facts)["attendee"]["email"] == "alex@example.com"


@pytest.mark.asyncio
async def test_partial_facts_allow_resolved_attendee_and_equivalent_time_zone(account):
    user, vault = account
    original = submit(user, "Book Alex at 10am Singapore tomorrow, alex@example.com, 30 minutes")
    start_test_run(original["run_id"], resolve_inbox=False)
    calendar = Calendar()
    executor = Executor(vault, lambda key: calendar)
    resolved = await executor.execute(
        original["run_id"],
        "resolve_requests",
        {
            "message_id": original["message_id"],
            "resolutions": [
                {
                    "relation": "new",
                    "reason": "Explicit booking in Singapore time",
                    "facts": {
                        "start": "2026-11-06T10:00:00+08:00",
                        "duration_minutes": 30,
                        "attendee": {"email": "alex@example.com"},
                    },
                }
            ],
        },
    )
    task = resolved[0]
    args = {**facts(2), "request_id": task["request_id"], "request_revision": task["revision"]}
    result = await executor.execute(original["run_id"], "create_booking", args)
    assert result["response"]["uid"] == "uid_1"
    assert len(calendar.posts) == 1


@pytest.mark.asyncio
async def test_new_writes_cannot_skip_durable_identity_by_dismissing_message(account):
    user, vault = account
    accepted = submit(user, "Book Alex tomorrow at ten")
    start_test_run(accepted["run_id"], resolve_inbox=False)
    calendar = Calendar()
    executor = Executor(vault, lambda key: calendar)
    await executor.execute(
        accepted["run_id"],
        "resolve_requests",
        {
            "message_id": accepted["message_id"],
            "resolutions": [
                {"relation": "dismiss", "reason": "Deliberately incorrect fixture interpretation"}
            ],
        },
    )
    result = await executor.execute(accepted["run_id"], "create_booking", facts(10))
    assert result["code"] == "request_conflict" and "request_id" in result["error"]
    assert not calendar.posts


@pytest.mark.asyncio
async def test_reschedule_revalidates_preserved_duration_against_live_booking(account):
    user, vault = account
    first = submit(user, "Book Alex at ten for thirty minutes")
    start_test_run(first["run_id"], resolve_inbox=False)
    calendar = Calendar()
    executor = Executor(vault, lambda key: calendar)
    task = (
        await executor.execute(
            first["run_id"],
            "resolve_requests",
            {
                "message_id": first["message_id"],
                "resolutions": [
                    {"relation": "new", "facts": facts(10), "reason": "Original request"}
                ],
            },
        )
    )[0]
    await executor.execute(
        first["run_id"],
        "create_booking",
        {
            **facts(10),
            "request_id": task["request_id"],
            "request_revision": 1,
        },
    )
    calendar.bookings["uid_1"]["end"] = "2026-11-06T11:00:00Z"
    corrected = submit(user, "Let's book this for noon instead")
    start_test_run(corrected["run_id"], resolve_inbox=False)
    await executor.execute(
        corrected["run_id"],
        "resolve_requests",
        {
            "message_id": corrected["message_id"],
            "resolutions": [
                {
                    "relation": "amend",
                    "request_id": task["request_id"],
                    "expected_revision": 1,
                    "facts": {"start": facts(12)["start"]},
                    "reason": "Time-only correction",
                }
            ],
        },
    )
    result = await executor.execute(
        corrected["run_id"],
        "reschedule_booking",
        {
            "booking_uid": "uid_1",
            "new_start": facts(12)["start"],
            "request_id": task["request_id"],
            "request_revision": 2,
        },
    )
    assert "live duration differs" in result["error"] and len(calendar.posts) == 1
