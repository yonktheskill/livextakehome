from uuid import uuid4

import httpx
import pytest

from app.calcom.client import CalClient
from app.db.models import Event
from app.db.repository import accept
from app.tools.executor import Executor, UnknownOutcome, Waiting

BOOKING = {
    "uid": "abc",
    "title": "Intro",
    "start": "2026-11-05T06:00:00Z",
    "end": "2026-11-05T06:30:00Z",
    "status": "accepted",
    "hosts": [{"id": 42}],
    "attendees": [{"name": "Alice"}],
    "eventTypeId": 10,
}
TYPE = {
    "id": 10,
    "ownerId": 42,
    "lengthInMinutes": 30,
    "lengthInMinutesOptions": [30, 45, 60],
    "locations": [{"type": "integration"}],
}
ARGS = {
    "event_type_id": 10,
    "start": "2026-11-06T06:00:00Z",
    "duration_minutes": 44,
    "attendee": {"name": "Alice", "email": "alice@example.com", "time_zone": "Asia/Singapore"},
}


def fixture_executor(vault, posts, *, timeout=False, booking=None):
    async def handler(request):
        path = request.url.path
        if request.method == "POST":
            posts.append(request)
            if timeout:
                raise httpx.ReadTimeout("private detail", request=request)
            return httpx.Response(
                200,
                json={"status": "success", "data": {**BOOKING, "uid": "replacement", "id": 100}},
            )
        if path == "/v2/event-types/10":
            assert request.headers["cal-api-version"] == "2026-06-12"
            data = TYPE
        elif path == "/v2/bookings/abc":
            data = booking or BOOKING
        elif path == "/v2/slots":
            assert request.headers["cal-api-version"] == "2024-09-04"
            data = {"2026-11-06": [{"start": "2026-11-06T06:00:00Z"}]}
        elif path == "/v2/schedules":
            data = [{"id": 7, "isDefault": True}]
        elif path == "/v2/bookings":
            data = [BOOKING]
        else:
            data = []
        return httpx.Response(200, json={"status": "success", "data": data})

    return Executor(vault, lambda key: CalClient(key, httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_create_rounding_reschedule_and_list(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts)
    run = accept(user, "book", str(uuid4()))["run_id"]
    result = await executor.execute(run, "create_booking", ARGS)
    assert result["validated"]["effective_duration_minutes"] == 45
    assert len(posts) == 1 and b'"lengthInMinutes":45' in posts[0].content
    run = accept(user, "move", str(uuid4()))["run_id"]
    result = await executor.execute(
        run, "reschedule_booking", {"booking_uid": "abc", "new_start": "2026-11-06T06:00:00Z"}
    )
    assert result["response"]["uid"] == "replacement" and len(posts) == 2
    result = await executor.execute(
        run, "list_bookings", {"start": "2026-11-05T00:00:00Z", "end": "2026-11-06T00:00:00Z"}
    )
    assert result["bookings"][0]["uid"] == "abc"


@pytest.mark.asyncio
async def test_exact_cancel_confirmation_and_changed_target(account):
    user, vault = account
    user.time_zone = "Asia/Singapore"
    user.save()
    posts = []
    executor = fixture_executor(vault, posts)
    run = accept(user, "cancel", str(uuid4()))["run_id"]
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    assert "cancel “Intro”" in str(waiting.value)
    assert "Thursday, 5 November 2026 at 14:00 (Asia/Singapore)" in str(waiting.value)
    assert "Thursday, 5 November 2026 at 14:30 (Asia/Singapore)" in str(waiting.value)
    assert "**Booking reference:** abc" in str(waiting.value)
    assert '{"' not in str(waiting.value)
    assert not posts
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    result = await executor.execute(confirmed, "cancel_booking", {"booking_uid": "abc"})
    assert "error" not in result and len(posts) == 1
    run = accept(user, "cancel again", str(uuid4()))["run_id"]
    with pytest.raises(Waiting):
        await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    changed = fixture_executor(vault, posts, booking={**BOOKING, "title": "Changed"})
    with pytest.raises(Waiting):
        await changed.execute(confirmed, "cancel_booking", {"booking_uid": "abc"})
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_event_type_creation_requires_permission_and_uses_default_schedule(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts)
    run = accept(user, "need a missing type", str(uuid4()))["run_id"]
    args = {"title": "Consultation", "duration_minutes": 45}
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "create_event_type", args)
    text = str(waiting.value)
    assert "Shall I create “Consultation” as a reusable meeting type?" in text
    assert "**Duration:** 45 minutes" in text
    assert "**Location:** Cal Video" in text
    assert "**Availability:** Your default schedule" in text
    assert "**Minimum notice:** 1 hour" in text
    assert "Reply **yes**" in text
    assert all(key not in text for key in ("schedule_id", "duration_minutes", '{"'))
    assert not Event.select().where(Event.kind == "tool.status").exists()
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    result = await executor.execute(confirmed, "create_event_type", args)
    assert "error" not in result
    assert b'"scheduleId":7' in posts[0].content
    assert posts[0].headers["cal-api-version"] == "2026-06-12"


@pytest.mark.asyncio
async def test_unknown_no_resend_missing_fields_and_end_constraint(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, timeout=True)
    run = accept(user, "book", str(uuid4()))["run_id"]
    result = await executor.execute(run, "create_booking", {**ARGS, "attendee": {"name": "Alice"}})
    assert "error" in result and not posts
    result = await executor.execute(
        run, "create_booking", {**ARGS, "explicit_end": "2026-11-06T06:35:00Z"}
    )
    assert "error" in result and not posts
    with pytest.raises(UnknownOutcome):
        await executor.execute(run, "create_booking", ARGS)
    assert len(posts) == 1
    from app.tools.executor import Interrupted

    with pytest.raises(Interrupted):
        await executor.execute(run, "create_booking", ARGS)
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_explicit_type_request_and_mutation_deduplication(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts)
    run = accept(user, "create a 45-minute Consultation type", str(uuid4()))["run_id"]
    args = {"title": "Consultation", "duration_minutes": 45}
    result = await executor.execute(run, "create_event_type", args)
    assert "error" not in result and len(posts) == 1
    assert await executor.execute(run, "create_event_type", args) == result
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_cursor_pagination_and_cross_midnight_overlap(account):
    user, vault = account
    seen = []

    async def handler(request):
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {**BOOKING, "start": "2026-11-04T23:30:00Z", "end": "2026-11-05T00:30:00Z"}
                    ],
                    "pagination": {"hasMore": True, "nextCursor": "page-two"},
                },
            )
        return httpx.Response(200, json={"data": [], "pagination": {"nextCursor": None}})

    executor = Executor(vault, lambda key: CalClient(key, httpx.MockTransport(handler)))
    run = accept(user, "list tomorrow", str(uuid4()))["run_id"]
    result = await executor.execute(
        run, "list_bookings", {"start": "2026-11-05T00:00:00Z", "end": "2026-11-06T00:00:00Z"}
    )
    assert len(result["bookings"]) == 1 and not result["truncated"]
    assert seen[1]["cursor"] == "page-two" and seen[0]["limit"] == "100"
    assert "afterStart" not in seen[0] and "beforeEnd" not in seen[0]


@pytest.mark.asyncio
async def test_event_type_list_keeps_scheduling_fields_without_unrelated_metadata(account):
    user, vault = account
    event_type = {
        **TYPE,
        "title": "Consultation",
        "bookingFields": [{"slug": "purpose", "required": True}],
        "minimumBookingNotice": 60,
        "confirmationPolicy": {"type": "always"},
        "users": [{"email": "owner@example.com", "avatar": "x" * 10000}],
        "metadata": {"unrelated": "x" * 10000},
    }

    def handler(request):
        assert request.url.path == "/v2/event-types"
        return httpx.Response(200, json={"status": "success", "data": [event_type]})

    executor = Executor(vault, lambda key: CalClient(key, httpx.MockTransport(handler)))
    run_id = accept(user, "book a meeting", str(uuid4()))["run_id"]
    result = await executor.execute(run_id, "list_event_types", {})
    assert result == [{k: v for k, v in event_type.items() if k not in {"users", "metadata"}}]
