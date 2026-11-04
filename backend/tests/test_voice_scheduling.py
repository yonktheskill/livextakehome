"""Voice captions through runner, real dispatch and mocked Cal.com HTTP."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from conftest import start_test_run
from test_voice import delegation, session, transcript

from app.agent.runner import Runner
from app.calcom.client import CalClient
from app.core.config import Settings
from app.db import voice
from app.db.models import Action, BookingRequest, Event, Message, RequestInbox, Run
from app.db.repository import accept, finish
from app.db.requests import resolve_requests
from app.providers.base import ProviderEvent
from app.tools.executor import Executor


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reschedule_booking", "cancel_booking"])
@pytest.mark.parametrize("later_typed_request", [False, True])
async def test_multi_segment_voice_can_move_or_cancel_booking(
    account, operation, later_typed_request
):
    user, vault = account
    user.time_zone = "Asia/Dubai"
    user.save()
    posts, slot_reads, bookings = [], [], {}

    async def calendar(request):
        path = request.url.path
        if request.method == "POST":
            assert request.headers["cal-api-version"] == "2026-02-25"
            payload = json.loads(request.content)
            posts.append((path, payload))
            if path.endswith("/cancel") and not payload.get("cancellationReason", "").strip():
                return httpx.Response(
                    400,
                    json={
                        "status": "error",
                        "error": {"message": "Cancellation reason is required"},
                    },
                )
            if path == "/v2/bookings":
                row = {
                    "uid": "alice-original",
                    "title": "Meeting with Alice",
                    "eventTypeId": 10,
                    "hosts": [{"id": 42}],
                    "attendees": [payload["attendee"]],
                }
            else:
                row = bookings["alice-original"].copy()
            if path.endswith("/cancel"):
                row["status"] = "cancelled"
            else:
                start = datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
                row.update(
                    start=payload["start"],
                    end=(start + timedelta(minutes=15)).isoformat(),
                    status="accepted",
                )
                if path.endswith("/reschedule"):
                    row.update(uid="alice-moved", rescheduledFromUid="alice-original")
            bookings[row["uid"]] = row
            return httpx.Response(200, json={"status": "success", "data": row})
        if path == "/v2/event-types/10":
            data = {"id": 10, "ownerId": 42, "lengthInMinutes": 15}
        elif path == "/v2/slots":
            slot_reads.append(dict(request.url.params))
            data = {"2026-11-06": [{"start": request.url.params["start"]}]}
        else:
            data = bookings[path.rsplit("/", 1)[1]]
        return httpx.Response(200, json={"status": "success", "data": data})

    executor = Executor(vault, lambda key: CalClient(key, httpx.MockTransport(calendar)))
    original = accept(user, "Book Alice on 6 November at 12:30 for 15 minutes", str(uuid4()))
    start_test_run(original["run_id"], resolve_inbox=False)
    facts = {
        "event_type_id": 10,
        "start": "2026-11-06T12:30:00+04:00",
        "duration_minutes": 15,
        "attendee": {"name": "Alice", "email": "alice@example.com", "time_zone": "Asia/Dubai"},
    }
    task = resolve_requests(
        original["run_id"],
        original["message_id"],
        [{"relation": "new", "facts": facts, "reason": "Explicit booking request"}],
    )[0]
    booked = await executor.execute(
        original["run_id"],
        "create_booking",
        {**facts, "request_id": task["request_id"], "request_revision": 1},
    )
    assert booked["response"]["uid"] == "alice-original"
    finish(original["run_id"], "succeeded", "run.completed", {})

    call = session(user)
    call.created_at -= 10  # Transcript offsets belong before the later typed request.
    call.save()
    voice.fragment(call.id, transcript("filler", "Oh yeah, I cannot", 100, 500))
    command = (
        "Move Alice's meeting on 6 November from 12:30–12:45 to 12:45–1 pm Asia/Dubai"
        if operation == "reschedule_booking"
        else "Cancel Alice's whole meeting on 6 November, 12:30–12:45 pm Asia/Dubai"
    )
    voice.fragment(call.id, transcript("command", command, 4000, 4500))
    run_id, _ = voice.delegate(call.id, delegation(cutoff=5000))
    source_id = Run.get_by_id(run_id).message_id
    if later_typed_request:
        run_id = accept(user, "Please apply that change", str(uuid4()))["run_id"]
    start_test_run(run_id, resolve_inbox=False)
    expected = [i.message_id for i in RequestInbox.select().where(RequestInbox.state == "pending")]
    arguments = {"booking_uid": "alice-original"}
    if operation == "reschedule_booking":
        arguments.update(
            new_start="2026-11-06T12:45:00+04:00",
            request_id=task["request_id"],
            request_revision=2,
        )

    class Decisions:
        step = 0

        async def stream(self, messages, tools):
            self.step += 1

            def tool(name, args, suffix=""):
                return ProviderEvent(
                    "tool_call", name=name, arguments=args, call_id=f"{self.step}{suffix}"
                )

            if self.step == 1:
                yield tool("request_context", {})
            elif self.step == 2:
                context = json.loads(messages[-1]["content"])
                assert [item["message_id"] for item in context["inbox"]] == expected
                for index, item in enumerate(context["inbox"]):
                    resolution = {"relation": "dismiss", "reason": "No new booking task"}
                    if item["message_id"] == source_id and operation == "reschedule_booking":
                        resolution.update(
                            relation="amend",
                            request_id=task["request_id"],
                            expected_revision=1,
                            facts={"start": arguments["new_start"]},
                            reason="Move the exact existing Alice booking, preserving duration",
                        )
                    yield tool(
                        "resolve_requests",
                        {"message_id": item["message_id"], "resolutions": [resolution]},
                        str(index),
                    )
            elif self.step == 3 and operation == "cancel_booking":
                yield tool(
                    "authorize_action",
                    {
                        "name": operation,
                        "arguments": arguments,
                        "source_message_id": source_id,
                        "reason": "Original speech explicitly cancels this whole Alice booking",
                    },
                )
            elif self.step == (4 if operation == "cancel_booking" else 3):
                yield tool(operation, arguments)
            else:
                assert "response" in json.loads(messages[-1]["content"])
                yield ProviderEvent("text_delta", text="The requested change is complete.")

    runner = Runner(Settings(llm_provider="fake"), vault, Decisions())
    runner.executor = executor
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "succeeded"
    assert not RequestInbox.select().where(RequestInbox.state == "pending").exists()
    action = Action.get(Action.run == run_id, Action.kind == operation)
    assert action.state == "succeeded"
    assert len(posts) == 2
    assert posts[-1][0] == (
        "/v2/bookings/alice-original/reschedule"
        if operation == "reschedule_booking"
        else "/v2/bookings/alice-original/cancel"
    )
    result = json.loads(action.result)
    assert result["response"]["attendees"] == booked["response"]["attendees"]
    if operation == "reschedule_booking":
        assert posts[-1][1] == {
            "start": "2026-11-06T08:45:00Z",
            "reschedulingReason": "User requested rescheduling",
        }
        assert result["response"]["end"] == "2026-11-06T09:00:00+00:00"
        assert slot_reads[-1]["bookingUidToReschedule"] == "alice-original"
        assert BookingRequest.get_by_id(task["request_id"]).booking_uid == "alice-moved"
    else:
        assert result["response"]["status"] == "cancelled"
        assert json.loads(action.request)["user_authorization"]["source_message_id"] == source_id
    replay = await executor.execute(run_id, operation, arguments)
    if operation == "reschedule_booking":
        assert replay["code"] == "request_conflict"  # The completed task cannot be resumed.
    else:
        assert replay == result
    assert len(posts) == 2  # Replaying a successful call never dispatches another write.
    assert Event.select().where(Event.run == run_id, Event.kind == "action.result").count() == 1
    assert Message.get(Message.client_request_id == f"action:{action.id}")
