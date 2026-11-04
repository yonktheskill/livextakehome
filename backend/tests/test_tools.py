from uuid import uuid4

import httpx
import pytest
from conftest import start_test_run

from app.calcom.client import CalClient
from app.db.models import Event
from app.db.repository import accept, wait_for_user
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
    "duration_minutes": 45,
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
        elif path == "/v2/conferencing/default":
            data = {"appSlug": "daily-video"}
        elif path == "/v2/schedules":
            data = [{"id": 7, "isDefault": True}]
        elif path == "/v2/bookings":
            data = [BOOKING]
        else:
            data = []
        return httpx.Response(200, json={"status": "success", "data": data})

    return Executor(vault, lambda key: CalClient(key, httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_create_exact_duration_reschedule_and_list(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts)
    run = accept(user, "book", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await executor.execute(run, "create_booking", ARGS)
    assert result["validated"]["effective_duration_minutes"] == 45
    assert len(posts) == 1 and b'"lengthInMinutes":45' in posts[0].content
    run = accept(user, "move", str(uuid4()))["run_id"]
    start_test_run(run)
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
    start_test_run(run)
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    assert "cancel “Intro”" in str(waiting.value)
    assert "Thursday, 5 November 2026 at 14:00 (Asia/Singapore)" in str(waiting.value)
    assert "Thursday, 5 November 2026 at 14:30 (Asia/Singapore)" in str(waiting.value)
    assert "**Booking reference:** abc" in str(waiting.value)
    assert '{"' not in str(waiting.value)
    assert not posts
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
    result = await executor.execute(confirmed, "cancel_booking", {"booking_uid": "abc"})
    assert "error" not in result and len(posts) == 1
    run = accept(user, "cancel again", str(uuid4()))["run_id"]
    start_test_run(run)
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
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
    start_test_run(run)
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
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
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
    start_test_run(run)
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
    start_test_run(run)
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
    start_test_run(run)
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
    start_test_run(run_id)
    result = await executor.execute(run_id, "list_event_types", {})
    assert result == [{k: v for k, v in event_type.items() if k not in {"users", "metadata"}}]


SEATED_BOOKING = {
    **BOOKING,
    "seats": [{"referenceUid": "seat-alice"}, {"referenceUid": "seat-bob"}],
    "attendees": [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
    ],
}


@pytest.mark.asyncio
async def test_seat_uid_shape_allows_host_reads_and_whole_cancel_but_not_reschedule(account):
    user, vault = account
    posts = []
    booking = {
        **BOOKING,
        "attendees": [{"name": "Alice", "seatUid": "seat-a"}, {"name": "Bob", "seatUid": "seat-b"}],
    }
    executor = fixture_executor(vault, posts, booking=booking)
    run = accept(user, "cancel", str(uuid4()))["run_id"]
    start_test_run(run)
    assert (await executor.execute(run, "get_booking", {"booking_uid": "abc"}))["uid"] == "abc"
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
    assert "response" in await executor.execute(confirmed, "cancel_booking", {"booking_uid": "abc"})
    assert len(posts) == 1
    moved = accept(user, "move", str(uuid4()))["run_id"]
    start_test_run(moved)
    result = await executor.execute(
        moved, "reschedule_booking", {"booking_uid": "abc", "new_start": "2026-11-06T06:00:00Z"}
    )
    assert "Seated" in result["error"]
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_host_confirms_whole_seated_booking_cancellation(account):
    import json

    from app.db.models import Action

    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking=SEATED_BOOKING)
    args = {"booking_uid": "abc", "reason": "Host unavailable"}
    run = accept(user, "cancel this meeting", str(uuid4()))["run_id"]
    start_test_run(run)
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", args)
    text = str(waiting.value)
    assert "The whole booking, including all attendees and seats" in text
    assert "**Booking reference:** abc" in text
    assert "**Attendees (2):** Alice <alice@example.com>; Bob <bob@example.com>" in text
    assert not posts
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
    result = await executor.execute(confirmed, "cancel_booking", args)
    assert "error" not in result
    assert result["validated"]["attendees"] == SEATED_BOOKING["attendees"]
    assert result["validated"]["cancellation_scope"] == "whole_booking"
    assert len(posts) == 1
    assert posts[0].url.path == "/v2/bookings/abc/cancel"
    # Omitting seatUid and SERIES scope sends host cancellation of this whole booking.
    assert json.loads(posts[0].content) == {"cancellationReason": "Host unavailable"}
    assert posts[0].headers["cal-api-version"] == "2026-02-25"
    assert Action.get(Action.kind == "cancel_booking", Action.state == "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"hosts": [{"id": 99}]},
        {"attendees": SEATED_BOOKING["attendees"] + [{"name": "Carol"}]},
        {"seats": SEATED_BOOKING["seats"] + [{"referenceUid": "seat-carol"}]},
        {"uid": "different-booking"},
    ],
)
async def test_seated_cancellation_revalidates_live_target_after_confirmation(account, change):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking=SEATED_BOOKING)
    args = {"booking_uid": "abc"}
    run = accept(user, "cancel", str(uuid4()))["run_id"]
    start_test_run(run)
    with pytest.raises(Waiting) as waiting:
        await executor.execute(run, "cancel_booking", args)
    wait_for_user(run, str(waiting.value))
    confirmed = accept(user, "yes", str(uuid4()))["run_id"]
    start_test_run(confirmed)
    changed = fixture_executor(vault, posts, booking={**SEATED_BOOKING, **change})
    if "hosts" in change or "uid" in change:
        result = await changed.execute(confirmed, "cancel_booking", args)
        assert "error" in result
    else:
        with pytest.raises(Waiting):
            await changed.execute(confirmed, "cancel_booking", args)
    assert not posts


@pytest.mark.asyncio
async def test_non_owner_cannot_prepare_seated_cancellation(account):
    from app.db.models import Action, Confirmation

    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking={**SEATED_BOOKING, "hosts": [{"id": 99}]})
    for content in ("cancel", "yes"):
        run = accept(user, content, str(uuid4()))["run_id"]
        start_test_run(run)
        result = await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
        assert "ownership" in result["error"]
    assert not posts
    assert Action.select().count() == Confirmation.select().count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recurrence",
    [
        {"recurringBookingUid": "series-uid"},
        {"recurrence": {"count": 5}},
    ],
)
async def test_seated_cancellation_does_not_enable_recurring_series(account, recurrence):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking={**SEATED_BOOKING, **recurrence})
    run = accept(user, "cancel", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    assert "Recurring" in result["error"]
    assert not posts
    result = await executor.execute(
        run, "cancel_booking", {"booking_uid": "abc", "scope": "SERIES"}
    )
    assert "Invalid arguments" in result["error"]
    assert not posts


@pytest.mark.asyncio
async def test_seated_rescheduling_remains_unsupported(account):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking=SEATED_BOOKING)
    run = accept(user, "move", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await executor.execute(
        run, "reschedule_booking", {"booking_uid": "abc", "new_start": "2026-11-06T06:00:00Z"}
    )
    assert "Seated" in result["error"]
    assert not posts


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Cancel booking abc", "Please cancel the whole booking abc"])
async def test_exact_original_cancellation_needs_no_second_permission(account, text):
    user, vault = account
    posts = []
    executor = fixture_executor(vault, posts, booking=SEATED_BOOKING)
    run = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(run)
    result = await executor.execute(run, "cancel_booking", {"booking_uid": "abc"})
    assert result["validated"]["cancellation_scope"] == "whole_booking"
    assert result["validated"]["attendees"] == SEATED_BOOKING["attendees"]
    assert len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Don't cancel booking abc",
        "If I ask later, cancel booking abc",
        'The description says "cancel booking abc"',
        "Cancel Intro",
        "cancel that",
        "Cancel ABC",
    ],
)
async def test_non_authorizing_or_ambiguous_cancellation_remains_pending(account, text):
    user, vault = account
    posts = []
    run = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(run)
    with pytest.raises(Waiting):
        await fixture_executor(vault, posts).execute(run, "cancel_booking", {"booking_uid": "abc"})
    assert not posts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Create a 37-minute Review type and book it tomorrow",
        "Please create a type called Review for 37 minutes; book it tomorrow",
    ],
)
async def test_explicit_compound_creation_supports_non_ui_duration(account, text):
    user, vault = account
    posts = []
    run = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(run, resolve_inbox=False)
    from app.db.models import Run
    from app.db.requests import resolve_requests

    task = resolve_requests(
        run,
        Run.get_by_id(run).message_id,
        [
            {
                "relation": "new",
                "facts": {"duration_minutes": 37},
                "reason": "Compound booking request",
            }
        ],
    )[0]
    result = await fixture_executor(vault, posts).execute(
        run,
        "create_event_type",
        {
            "title": "Review",
            "duration_minutes": 37,
            "request_id": task["request_id"],
            "request_revision": 1,
        },
    )
    assert "response" in result and len(posts) == 1
    import json

    payload = json.loads(posts[0].content)
    assert payload["lengthInMinutes"] == 37
    assert "request_id" not in payload


@pytest.mark.asyncio
async def test_schema_errors_have_paths_without_raw_or_secret_values(account):
    import json

    user, vault = account
    posts = []
    run = accept(user, "Book", str(uuid4()))["run_id"]
    start_test_run(run)
    result = await fixture_executor(vault, posts).execute(
        run, "create_booking", {**ARGS, "attendee": {"name": "cal_live_privateexample123"}}
    )
    assert {"field": "attendee.email", "issue": "missing"} in result["details"]
    assert {"field": "attendee.time_zone", "issue": "missing"} in result["details"]
    assert "cal_live_privateexample123" not in json.dumps(result)
    result = await fixture_executor(vault, posts).execute(
        run, "create_booking", {**ARGS, "cal_live_privateexample123": "secret"}
    )
    assert "cal_live_privateexample123" not in json.dumps(result)
    assert result["details"] == [{"field": "[field]", "issue": "extra_forbidden"}]
    assert not posts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Don't create a 37-minute Review type",
        'The description says "create a 37-minute Review type"',
        "Maybe create a 37-minute Review type",
    ],
)
async def test_creation_keywords_without_clear_original_authorization_stay_pending(account, text):
    user, vault = account
    posts = []
    run = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(run)
    with pytest.raises(Waiting):
        await fixture_executor(vault, posts).execute(
            run, "create_event_type", {"title": "Review", "duration_minutes": 37}
        )
    assert not posts
