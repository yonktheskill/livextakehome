import json
from uuid import uuid4

import httpx
import pytest
from conftest import start_test_run

from app.calcom.client import CalClient, CalError
from app.db.models import Action
from app.db.repository import accept, wait_for_user
from app.tools.durations import duration_plan
from app.tools.executor import Executor, Waiting


def event_type(minutes, type_id=10):
    return {
        "id": type_id,
        "ownerId": 42,
        "title": "Meeting",
        "lengthInMinutes": minutes,
        "lengthInMinutesOptions": [minutes],
    }


@pytest.mark.parametrize(
    ("types", "requested", "action", "effective", "ids"),
    [
        ([event_type(30)], 30, "reuse", 30, [10]),
        ([event_type(40)], 30, "create_event_type", 30, []),
        ([], 30, "create_event_type", 30, []),
        ([], 110, "create_event_type", 110, []),
        ([event_type(150)], 110, "create_event_type", 110, []),
        ([event_type(120)], 110, "create_event_type", 110, []),
        ([event_type(110)], 110, "reuse", 110, [10]),
        ([event_type(115)], 110, "create_event_type", 110, []),
        ([event_type(500)], 490, "create_event_type", 490, []),
        ([event_type(40), event_type(30, 11)], 30, "reuse", 30, [11]),
        ([{**event_type(40), "lengthInMinutesOptions": [30, 40]}], 30, "reuse", 30, [10]),
        ([{**event_type(40), "title": "30 min meeting"}], 30, "create_event_type", 30, []),
    ],
)
def test_duration_precedence(types, requested, action, effective, ids):
    plan = duration_plan(types, requested)
    assert plan == {
        "action": action,
        "requested_duration_minutes": requested,
        "effective_duration_minutes": effective,
        "rounded": effective != requested,
        "event_type_ids": ids,
        "creation_requires_confirmation": action == "create_event_type",
    }


@pytest.mark.parametrize(("requested", "maximum"), [(110, 109), (1441, None), (30, 25)])
def test_invalid_duration_or_exceeded_maximum(requested, maximum):
    with pytest.raises(CalError):
        duration_plan([], requested, maximum)


class Calendar:
    def __init__(self, existing):
        self.types = {10: event_type(existing)}
        self.posts = []
        self.slot_durations = []

    def handler(self, request):
        path = request.url.path
        if request.method == "POST":
            payload = json.loads(request.content)
            self.posts.append((path, payload))
            if path == "/v2/event-types":
                data = event_type(payload["lengthInMinutes"], 11)
                self.types[11] = data
            else:
                minutes = self.types[payload["eventTypeId"]]["lengthInMinutes"]
                data = {
                    "uid": "created-booking",
                    "start": "2026-11-06T06:00:00Z",
                    "end": f"2026-11-06T{6 + minutes // 60:02}:{minutes % 60:02}:00Z",
                    "status": "accepted",
                }
        elif path == "/v2/conferencing/default":
            data = {"appSlug": "daily-video"}
        elif path == "/v2/schedules":
            data = [{"id": 7, "isDefault": True}]
        elif path == "/v2/slots":
            self.slot_durations.append(int(request.url.params["duration"]))
            data = {"2026-11-06": [{"start": "2026-11-06T06:00:00Z"}]}
        else:
            data = self.types[int(path.rsplit("/", 1)[1])]
        return httpx.Response(200, json={"status": "success", "data": data})

    def executor(self, vault):
        return Executor(vault, lambda key: CalClient(key, httpx.MockTransport(self.handler)))


BOOKING_ARGS = {
    "event_type_id": 11,
    "start": "2026-11-06T06:00:00Z",
    "attendee": {"name": "Alice", "email": "alice@example.com", "time_zone": "UTC"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "existing", "effective"), [(30, 40, 30), (110, 150, 110)])
async def test_create_missing_type_confirm_then_book_original_request(
    account, requested, existing, effective
):
    user, vault = account
    calendar = Calendar(existing)
    executor = calendar.executor(vault)
    run = accept(user, f"Book a {requested}-minute meeting", str(uuid4()))["run_id"]
    start_test_run(run)
    plan = await executor.execute(
        run, "plan_booking_duration", {"duration_minutes": requested, "event_type_ids": [10]}
    )
    assert plan["action"] == "create_event_type"
    assert plan["effective_duration_minutes"] == effective
    args = {
        "title": "Meeting",
        "duration_minutes": effective,
        "requested_duration_minutes": requested,
    }
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "create_event_type", args)
    if requested != effective:
        assert f"You asked for {requested} minutes" in str(waiting.value)
        assert f"next supported length is {effective} minutes" in str(waiting.value)
    assert not calendar.posts
    proposal = json.loads(Action.get().request)["validation"]
    assert proposal["duration_minutes"] == effective
    assert proposal["requested_duration_minutes"] == requested

    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]

    start_test_run(confirmed)
    created = await executor.execute(confirmed, "create_event_type", args)
    assert created["response"]["id"] == 11
    plan = await executor.execute(
        confirmed,
        "plan_booking_duration",
        {"duration_minutes": requested, "event_type_ids": [10, 11]},
    )
    assert plan["action"] == "reuse" and plan["event_type_ids"] == [11]
    slots = await executor.execute(
        confirmed,
        "get_available_slots",
        {
            "event_type_id": 11,
            "duration_minutes": requested,
            "start": BOOKING_ARGS["start"],
            "end": "2026-11-07T06:00:00Z",
        },
    )
    assert slots["effective_duration_minutes"] == effective
    booked = await executor.execute(
        confirmed, "create_booking", {**BOOKING_ARGS, "duration_minutes": requested}
    )
    assert booked["validated"]["requested_duration_minutes"] == requested
    assert booked["validated"]["effective_duration_minutes"] == effective
    assert calendar.slot_durations == [effective, effective]
    assert [path for path, _ in calendar.posts] == ["/v2/event-types", "/v2/bookings"]
    assert calendar.posts[-1][1]["lengthInMinutes"] == effective


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "existing"), [(30, 40), (110, 150)])
async def test_slot_and_booking_tools_cannot_skip_missing_shorter_type(
    account, requested, existing
):
    user, vault = account
    calendar = Calendar(existing)
    executor = calendar.executor(vault)
    run = accept(user, f"Book {requested} minutes", str(uuid4()))["run_id"]
    start_test_run(run)
    for tool, args in [
        ("create_booking", {**BOOKING_ARGS, "event_type_id": 10, "duration_minutes": requested}),
        (
            "get_available_slots",
            {
                "event_type_id": 10,
                "duration_minutes": requested,
                "start": BOOKING_ARGS["start"],
                "end": "2026-11-07T06:00:00Z",
            },
        ),
    ]:
        result = await executor.execute(run, tool, args)
        assert "create_event_type" in result["error"]
    assert not calendar.posts and not calendar.slot_durations


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "proposed"), [(30, 40), (110, 150), (110, 90)])
async def test_creation_cannot_change_requested_length(account, requested, proposed):
    user, vault = account
    calendar = Calendar(40)
    run = accept(user, "book a meeting", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await calendar.executor(vault).execute(
        run,
        "create_event_type",
        {"title": "Meeting", "duration_minutes": proposed, "requested_duration_minutes": requested},
    )
    assert "error" in result and not calendar.posts
    assert not Action.select().exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constraint",
    [
        {"maximum_minutes": 110},
        {"explicit_end": "2026-11-06T07:50:00Z"},
    ],
)
async def test_rounded_booking_still_respects_maximum_and_end(account, constraint):
    user, vault = account
    calendar = Calendar(120)
    run = accept(user, "book 110 minutes", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await calendar.executor(vault).execute(
        run,
        "create_booking",
        {**BOOKING_ARGS, "event_type_id": 10, "duration_minutes": 110, **constraint},
    )
    assert "error" in result
    assert not calendar.posts and not calendar.slot_durations


@pytest.mark.asyncio
async def test_planner_checks_live_ownership(account):
    user, vault = account
    calendar = Calendar(30)
    calendar.types[10]["ownerId"] = 99
    run = accept(user, "book 30 minutes", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await calendar.executor(vault).execute(
        run, "plan_booking_duration", {"duration_minutes": 30, "event_type_ids": [10]}
    )
    assert "ownership" in result["error"] and not calendar.posts
