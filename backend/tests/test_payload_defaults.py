"""Field-aware defaults keep incomplete metadata out of outgoing API payloads."""

import json
from uuid import uuid4

import pytest
from conftest import start_test_run
from pydantic import ValidationError
from test_scheduling_validation import ARGS, Calendar
from test_tools import BOOKING, fixture_executor

from app.calcom.client import CalError
from app.db.models import Action
from app.db.repository import accept
from app.tools.planning import prepare
from app.tools.schemas import Attendee, Cancel, CreateBooking, CreateType, Reschedule


class LiveDefaults(Calendar):
    def __init__(self, *, title="Consultation", **kwargs):
        super().__init__(**kwargs)
        self.title = title

    async def request(self, method, path, **kwargs):
        if path == "/v2/bookings/abc":
            return BOOKING
        result = await super().request(method, path, **kwargs)
        if path == "/v2/event-types/10":
            result["title"] = self.title
        return result


@pytest.mark.parametrize("blank", [None, "", "  ", []])
async def test_booking_defaults_missing_title_and_omits_blank_optional_answers(blank):
    fields = [
        {"slug": "title", "field": "title", "required": True},
        {"slug": "notes", "field": "notes"},
        {"slug": "attendeePhoneNumber", "field": "attendeePhoneNumber"},
        {"slug": "guests", "field": "guests"},
        {"slug": "consent", "type": "boolean", "required": True},
        {"slug": "count", "type": "number", "required": True},
    ]
    client = LiveDefaults(fields=fields)
    _, payload, details = await prepare(
        client,
        "create_booking",
        CreateBooking(
            **ARGS,
            booking_fields={
                "title": blank,
                "notes": blank,
                "attendeePhoneNumber": blank,
                "guests": blank,
                "consent": False,
                "count": 0,
            },
        ),
        42,
    )
    assert payload["bookingFieldsResponses"] == {
        "title": "Consultation",
        "consent": False,
        "count": 0,
    }
    assert "phoneNumber" not in payload["attendee"]
    assert "guests" not in payload
    assert details["booking_title"] == "Consultation"


@pytest.mark.parametrize("title", [None, "", "   "])
async def test_missing_live_title_uses_neutral_booking_title(title):
    _, payload, _ = await prepare(
        LiveDefaults(title=title, fields=[{"slug": "title", "field": "title", "required": True}]),
        "create_booking",
        CreateBooking(**ARGS),
        42,
    )
    assert payload["bookingFieldsResponses"]["title"] == "Meeting"


async def test_explicit_booking_metadata_is_preserved():
    fields = [{"slug": slug, "field": slug} for slug in ("title", "notes", "guests")]
    _, payload, _ = await prepare(
        LiveDefaults(fields=fields),
        "create_booking",
        CreateBooking(
            **ARGS,
            booking_fields={
                "title": "Budget review",
                "notes": "Discuss the figures",
                "guests": ["alice@example.com"],
            },
        ),
        42,
    )
    assert payload["bookingFieldsResponses"] == {
        "title": "Budget review",
        "notes": "Discuss the figures",
    }
    assert payload["guests"] == ["alice@example.com"]


async def test_standard_phone_field_uses_its_live_name_and_preserves_supplied_number():
    _, payload, _ = await prepare(
        LiveDefaults(fields=[{"slug": "attendeePhoneNumber", "field": "attendeePhoneNumber"}]),
        "create_booking",
        CreateBooking(**ARGS, booking_fields={"attendeePhoneNumber": "+6591234567"}),
        42,
    )
    assert payload["attendee"]["phoneNumber"] == "+6591234567"


@pytest.mark.parametrize(
    "locations",
    [
        [
            {"type": "integration", "integration": "cal-video"},
            {"type": "phone", "phone": "+6591234567"},
        ],
        [{"type": "attendeeAddress"}],
        [{"type": "integration", "integration": ""}],
    ],
)
async def test_required_location_is_not_guessed_when_the_default_is_unclear(locations):
    client = LiveDefaults(
        fields=[{"slug": "location", "field": "location", "required": True}], locations=locations
    )
    with pytest.raises(CalError, match="required booking field: location"):
        await prepare(client, "create_booking", CreateBooking(**ARGS, location="  "), 42)
    assert "/v2/slots" not in client.reads


async def test_unknown_blank_answers_are_rejected_instead_of_silently_discarded():
    with pytest.raises(CalError, match="Unknown booking field: missing"):
        await prepare(
            LiveDefaults(),
            "create_booking",
            CreateBooking(**ARGS, booking_fields={"missing": None}),
            42,
        )


@pytest.mark.parametrize("reason", [[], {}, False, 0])
def test_malformed_reason_types_are_rejected_instead_of_defaulted(reason):
    with pytest.raises(ValidationError):
        Cancel(booking_uid="abc", reason=reason)


@pytest.mark.parametrize("blank", [None, "", "   ", []])
async def test_required_custom_fields_still_need_an_answer(blank):
    client = LiveDefaults(fields=[{"slug": "purpose", "type": "text", "required": True}])
    with pytest.raises(CalError, match="required booking field: purpose"):
        await prepare(
            client, "create_booking", CreateBooking(**ARGS, booking_fields={"purpose": blank}), 42
        )
    assert "/v2/slots" not in client.reads


@pytest.mark.parametrize("description", [None, "", "   ", "An explicit description"])
async def test_empty_optional_type_description_is_omitted(description):
    _, payload, _ = await prepare(
        LiveDefaults(default={"appSlug": "cal-video"}),
        "create_event_type",
        CreateType(title="Review", duration_minutes=30, description=description),
        42,
    )
    if description == "An explicit description":
        assert payload["description"] == description
    else:
        assert "description" not in payload
    assert payload["beforeEventBuffer"] == payload["afterEventBuffer"] == 0


@pytest.mark.parametrize("location", [None, "", "   "])
async def test_single_configured_location_can_satisfy_required_location(location):
    _, payload, _ = await prepare(
        LiveDefaults(
            fields=[{"slug": "location", "field": "location", "required": True}],
            locations=[{"type": "integration", "integration": "cal-video"}],
        ),
        "create_booking",
        CreateBooking(**ARGS, location=location),
        42,
    )
    assert "location" not in payload  # Cal.com uses its sole configured location.
    assert "bookingFieldsResponses" not in payload


async def test_selected_location_omits_unused_empty_metadata_and_keeps_false():
    _, payload, _ = await prepare(
        LiveDefaults(
            locations=[
                {
                    "type": "phone",
                    "phone": "+6591234567",
                    "address": "  ",
                    "link": None,
                    "public": False,
                }
            ]
        ),
        "create_booking",
        CreateBooking(**ARGS, location="+6591234567"),
        42,
    )
    assert payload["location"] == {"type": "phone", "phone": "+6591234567", "public": False}


async def test_explicit_location_with_missing_required_data_fails_before_api_write():
    client = LiveDefaults(locations=[{"type": "integration", "integration": ""}])
    with pytest.raises(CalError, match="location is incomplete"):
        await prepare(client, "create_booking", CreateBooking(**ARGS, location="integration"), 42)
    assert "/v2/slots" not in client.reads


@pytest.mark.parametrize("kind", ["attendeeAddress", "attendeePhone", "attendeeDefined"])
async def test_attendee_defined_location_is_not_invented(kind):
    with pytest.raises(CalError, match="Attendee-defined locations"):
        await prepare(
            LiveDefaults(locations=[{"type": kind}]), "create_booking", CreateBooking(**ARGS), 42
        )


@pytest.mark.parametrize("reason", [None, "", "   ", "Schedule changed"])
async def test_action_reasons_are_nonempty_and_preserve_user_text(reason):
    for name, args, key, fallback in (
        (
            "cancel_booking",
            Cancel(booking_uid="abc", reason=reason),
            "cancellationReason",
            "User requested cancellation",
        ),
        (
            "reschedule_booking",
            Reschedule(booking_uid="abc", new_start=ARGS["start"], reason=reason),
            "reschedulingReason",
            "User requested rescheduling",
        ),
    ):
        _, payload, _ = await prepare(LiveDefaults(), name, args, 42)
        assert payload[key] == (reason if reason and reason.strip() else fallback)


@pytest.mark.parametrize("name", [None, "", "   "])
def test_attendee_identity_and_reusable_type_title_are_not_invented(name):
    with pytest.raises(ValidationError):
        Attendee(name=name, email="alice@example.com", time_zone="UTC")
    with pytest.raises(ValidationError):
        CreateType(title=name, duration_minutes=30)


async def test_null_and_blank_reasons_share_exact_authorization_evidence(account):
    user, vault = account
    accepted = accept(user, "Cancel the whole Intro meeting", str(uuid4()))
    start_test_run(accepted["run_id"])
    posts = []
    executor = fixture_executor(vault, posts)
    grant = await executor.execute(
        accepted["run_id"],
        "authorize_action",
        {
            "name": "cancel_booking",
            "arguments": {"booking_uid": "abc", "reason": None},
            "source_message_id": accepted["message_id"],
            "reason": "The user clearly cancels this whole meeting",
        },
    )
    assert grant["authorized"]
    result = await executor.execute(
        accepted["run_id"], "cancel_booking", {"booking_uid": "abc", "reason": "  "}
    )
    assert "response" in result and len(posts) == 1
    assert json.loads(posts[0].content)["cancellationReason"] == "User requested cancellation"
    assert json.loads(Action.get(Action.run == accepted["run_id"]).request)["user_authorization"]
