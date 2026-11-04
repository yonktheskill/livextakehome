"""Offline regressions for live scheduling validation and resumable reads."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import start_test_run
from pydantic import ValidationError

from app.calcom.client import CalError
from app.db.models import User
from app.db.repository import accept
from app.tools.executor import Executor
from app.tools.messages import confirmation_message
from app.tools.planning import prepare
from app.tools.schemas import CreateBooking, CreateType, ListBookings

ARGS = {
    "event_type_id": 10,
    "start": "2026-11-06T06:00:00Z",
    "attendee": {"name": "Bob", "email": "bob@example.com", "time_zone": "UTC"},
}
RANGE = {"start": "2026-11-05T00:00:00Z", "end": "2026-11-06T00:00:00Z"}


class Calendar:
    def __init__(self, *, fields=(), locations=(), default=None, accounts=()):
        self.fields, self.locations = fields, locations
        self.default = default
        self.accounts = accounts
        self.reads = []

    async def request(self, method, path, **kwargs):
        assert method == "GET"
        self.reads.append(path)
        return {
            "/v2/event-types/10": {
                "id": 10,
                "ownerId": 42,
                "lengthInMinutes": 30,
                "bookingFields": self.fields,
                "locations": self.locations,
            },
            "/v2/slots": {"day": [{"start": ARGS["start"]}]},
            "/v2/schedules": [{"id": 7, "isDefault": True}],
            "/v2/conferencing/default": self.default,
            "/v2/conferencing": self.accounts,
        }[path]


@pytest.mark.parametrize(
    ("slug", "credential", "integration", "label"),
    [
        ("daily-video", None, "cal-video", "Cal Video"),
        ("google-meet", "google_video", "google-meet", "Google Meet"),
        ("zoom", "zoom_video", "zoom", "Zoom"),
        ("office365-video", "office365_video", "office365-video", "Microsoft Teams"),
    ],
)
async def test_exact_default_location_payload_and_confirmation(
    slug, credential, integration, label
):
    client = Calendar(default={"appSlug": slug}, accounts=[{"type": credential, "invalid": False}])
    _, payload, details = await prepare(
        client, "create_event_type", CreateType(title="Intro", duration_minutes=30), 42
    )
    assert payload["locations"] == [{"type": "integration", "integration": integration}]
    assert details["location"] == label
    assert f"**Location:** {label}" in confirmation_message("create_event_type", details, "UTC")
    assert set(client.reads) <= {
        "/v2/schedules",
        "/v2/conferencing/default",
        "/v2/conferencing",
    }


@pytest.mark.parametrize(
    ("default", "accounts"),
    [
        (None, []),
        ({"appSlug": "unsupported"}, []),
        ({"appSlug": "zoom"}, []),
        ({"appSlug": "zoom"}, [{"type": "zoom_video", "invalid": True}]),
        ({"appSlug": "zoom"}, [{"type": "zoom_video", "invalid": False}] * 2),
    ],
)
async def test_missing_invalid_ambiguous_default_blocks_creation(default, accounts):
    with pytest.raises(CalError, match="Ask|ask"):
        await prepare(
            Calendar(default=default, accounts=accounts),
            "create_event_type",
            CreateType(title="Intro", duration_minutes=30),
            42,
        )


FIELDS = [
    {"slug": "name", "type": "name", "required": True},
    {"slug": "email", "type": "email", "required": True},
    {"slug": "topics", "type": "multiselect", "required": True, "options": ["a", "b"]},
    {"slug": "checks", "type": "checkbox", "required": True, "options": ["yes"]},
    {"slug": "consent", "type": "boolean", "required": True},
    {"slug": "count", "type": "number", "required": True},
    {"slug": "attendeePhoneNumber", "type": "phone", "required": True},
    {"slug": "guests", "type": "multiemail"},
]
ANSWERS = {
    "topics": ["a", "b"],
    "checks": ["yes"],
    "consent": False,
    "count": 0,
    "attendeePhoneNumber": "+6591234567",
    "guests": ["alice@example.com"],
}


async def test_typed_answers_and_standard_mapping_preserve_values():
    _, payload, _ = await prepare(
        Calendar(fields=FIELDS), "create_booking", CreateBooking(**ARGS, booking_fields=ANSWERS), 42
    )
    assert payload["bookingFieldsResponses"] == {
        "topics": ["a", "b"],
        "checks": ["yes"],
        "consent": False,
        "count": 0,
    }
    assert payload["attendee"]["phoneNumber"] == "+6591234567"
    assert payload["guests"] == ["alice@example.com"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"topics": []},
        {"topics": ["bad"]},
        {"topics": "a"},
        {"checks": True},
        {"consent": "false"},
        {"count": True},
        {"attendeePhoneNumber": " "},
        {"unknown": "answer"},
        {"name": "Alice"},
        {"guests": ["bad"]},
    ],
)
async def test_invalid_live_answers_block_before_slots(overrides):
    client = Calendar(fields=FIELDS)
    with pytest.raises(CalError):
        await prepare(
            client,
            "create_booking",
            CreateBooking(**ARGS, booking_fields={**ANSWERS, **overrides}),
            42,
        )
    assert "/v2/slots" not in client.reads


@pytest.mark.parametrize("value", ["", "   ", []])
async def test_required_text_is_nonempty(value):
    with pytest.raises(CalError):
        await prepare(
            Calendar(fields=[{"slug": "why", "type": "text", "required": True}]),
            "create_booking",
            CreateBooking(**ARGS, booking_fields={"why": value}),
            42,
        )


@pytest.mark.parametrize("value", [{"nested": "x"}, [True], "x" * 4001, float("inf"), 10**16])
def test_answer_schema_bounds(value):
    with pytest.raises(ValidationError):
        CreateBooking(**ARGS, booking_fields={"why": value})


async def test_phone_value_selects_exact_location_and_rejects_ambiguity():
    phone = {"type": "phone", "phone": "+6591234567"}
    client = Calendar(locations=[phone, {"type": "link", "link": "https://example.com"}])
    _, payload, _ = await prepare(
        client, "create_booking", CreateBooking(**ARGS, location=phone["phone"]), 42
    )
    assert payload["location"] == phone
    with pytest.raises(CalError, match="exact location"):
        await prepare(
            Calendar(locations=[phone, phone]),
            "create_booking",
            CreateBooking(**ARGS, location=phone["phone"]),
            42,
        )


async def test_resume_beyond_500_preserves_overlap_and_honest_empty_result():
    class Pages:
        async def request(self, method, path, *, params):
            index = int(params.get("cursor", "0"))
            assert "afterStart" not in params and "beforeEnd" not in params
            rows = (
                [
                    {
                        "uid": f"outside-{index}-{n}",
                        "hosts": [{"id": 42}],
                        "start": "2026-10-01T00:00:00Z",
                        "end": "2026-10-01T00:30:00Z",
                    }
                    for n in range(100)
                ]
                if index < 5
                else [
                    {
                        "uid": "crossing",
                        "hosts": [{"id": 42}],
                        "start": "2026-11-04T23:30:00Z",
                        "end": "2026-11-05T00:30:00Z",
                    }
                ]
            )
            return {
                "bookings": rows,
                "pagination": {"nextCursor": str(index + 1) if index < 5 else None},
            }

    first = await Executor.read(
        Pages(), "list_bookings", ListBookings(**RANGE), SimpleNamespace(owner=42)
    )
    assert first["bookings"] == [] and first["truncated"]
    assert first["next_cursor"] == "5" and "does not establish" in first["message"]
    second = await Executor.read(
        Pages(),
        "list_bookings",
        ListBookings(**RANGE, cursor=first["next_cursor"]),
        SimpleNamespace(owner=42),
    )
    assert second["bookings"][0]["uid"] == "crossing" and not second["truncated"]


@pytest.mark.parametrize(
    ("text", "name", "saved"),
    [
        ("Book with Alice; my name is Bob", "Alice", False),
        ("Book with Alice; my name is Bob", "Bob", True),
        ("Don't call me Alice", "Alice", False),
        ("My name is not Alice", "Alice", False),
        ("Call me Alice or Bob", "Alice", False),
        ("Call me Alice; my name is Bob", "Bob", False),
        ('She said "call me Alice"', "Alice", False),
        ("Please call me Bob", "Bob", True),
    ],
)
async def test_only_explicit_self_identification_saves_name(account, text, name, saved):
    user, vault = account
    run = accept(user, text, str(uuid4()))["run_id"]
    start_test_run(run)
    result = await Executor(vault).execute(run, "save_preferred_name", {"preferred_name": name})
    assert result.get("saved", False) == saved
    assert User.get_by_id(user.id).preferred_name == (name if saved else None)


@pytest.mark.parametrize(
    "pagination",
    [
        {"nextCursor": "same"},
        {"hasMore": True, "nextCursor": None},
        {"nextCursor": 123},
    ],
)
async def test_unusable_pagination_never_claims_complete(pagination):
    class Pages:
        async def request(self, *args, **kwargs):
            return {"bookings": [], "pagination": pagination}

    with pytest.raises(CalError):
        await Executor.read(
            Pages(),
            "list_bookings",
            ListBookings(**RANGE, cursor="same"),
            SimpleNamespace(owner=42),
        )


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("email", "bad"),
        ("url", "javascript:bad"),
        ("phone", "123"),
        ("multiemail", ["bad"]),
    ],
)
async def test_contact_field_formats(kind, value):
    with pytest.raises(CalError):
        await prepare(
            Calendar(fields=[{"slug": "contact", "type": kind}]),
            "create_booking",
            CreateBooking(**ARGS, booking_fields={"contact": value}),
            42,
        )
