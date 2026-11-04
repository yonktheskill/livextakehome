from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.tools.durations import CREATABLE_DURATIONS

DURATION_DESCRIPTION = (
    "Original duration requested by the user; do not replace with a rounded value."
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SavePreferredName(Arguments):
    preferred_name: str = Field(min_length=1, max_length=200)

    @field_validator("preferred_name")
    @classmethod
    def clean_name(cls, value):
        value = " ".join(value.split())
        if not value or any(not char.isprintable() for char in value):
            raise ValueError("Provide a nonempty printable name")
        return value


class Range(Arguments):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timestamp must include UTC offset; clarify ambiguous local times.")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < (self.end - self.start).total_seconds() <= 31 * 86400:
            raise ValueError("Date range must be positive and at most 31 days")
        return self


class ListBookings(Range):
    status: Literal["upcoming", "past", "cancelled", "unconfirmed"] | None = None


class Booking(Arguments):
    booking_uid: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")


class EventType(Arguments):
    event_type_id: int = Field(gt=0)


class PlanDuration(Arguments):
    duration_minutes: int = Field(gt=0, le=1440, description=DURATION_DESCRIPTION)
    event_type_ids: list[Annotated[int, Field(gt=0)]] = Field(
        max_length=20,
        description="IDs of suitable personal event types from live results; [] if none. "
        "Respect a user-selected type and do not include unrelated templates.",
    )
    maximum_minutes: int | None = Field(default=None, gt=0, le=1440)


class Slots(Range):
    event_type_id: int = Field(gt=0)
    duration_minutes: int | None = Field(
        default=None, gt=0, le=1440, description=DURATION_DESCRIPTION
    )
    booking_uid: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")


class Attendee(Arguments):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    time_zone: str

    @field_validator("time_zone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Use an IANA time zone") from None
        return value


class CreateBooking(EventType):
    start: datetime
    attendee: Attendee
    duration_minutes: int | None = Field(
        default=None, gt=0, le=1440, description=DURATION_DESCRIPTION
    )
    explicit_end: datetime | None = None
    maximum_minutes: int | None = Field(default=None, gt=0)
    booking_fields: dict[str, str] = Field(default_factory=dict)
    location: str | None = Field(default=None, max_length=1000)

    @field_validator("start")
    @classmethod
    def aware_start(cls, value):
        return Range.aware(value)

    @field_validator("explicit_end")
    @classmethod
    def aware_end(cls, value):
        return Range.aware(value) if value is not None else None


class Reschedule(Booking):
    new_start: datetime

    @field_validator("new_start")
    @classmethod
    def aware_start(cls, value):
        return Range.aware(value)


class Cancel(Booking):
    reason: str = Field(default="", max_length=500)


class CreateType(Arguments):
    title: str = Field(min_length=1, max_length=120)
    duration_minutes: int = Field(gt=0, le=1440)
    requested_duration_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1440,
        description="Original booking duration when creating a missing type.",
    )
    description: str = Field(default="", max_length=1000)


SCHEMAS: dict[str, type[Arguments]] = {
    "save_preferred_name": SavePreferredName,
    "list_bookings": ListBookings,
    "get_booking": Booking,
    "list_event_types": Arguments,
    "get_event_type": EventType,
    "plan_booking_duration": PlanDuration,
    "get_available_slots": Slots,
    "create_booking": CreateBooking,
    "reschedule_booking": Reschedule,
    "cancel_booking": Cancel,
    "create_event_type": CreateType,
}
DESCRIPTIONS = {
    "save_preferred_name": "Save the authenticated user's explicitly supplied form of address. "
    "The name must appear in the current user message. Never infer it from email or attendees. "
    "Works without a Cal.com connection; can update an existing preferred name.",
    "cancel_booking": "Prepare exact single-booking cancellation; always asks user confirmation.",
    "create_event_type": "Propose reusable personal type using default schedule and Cal Video. "
    "Implicit creation requires confirmation. Clear explicit creation can be authorized. "
    f"Application creation choices (minutes): {list(CREATABLE_DURATIONS)}. "
    "Use plan_booking_duration before creating a missing booking type; preserve the original "
    "booking length in requested_duration_minutes and use the effective length "
    "in duration_minutes. "
    "Creation alone does not book an appointment. Failures of schedule, permission or connection "
    "are not evidence that a different duration can be created; report the blocker.",
    "plan_booking_duration": "Resolve duration before availability or booking: reuse an exact "
    "suitable type, else create the exact duration if allowed, else round up to the smallest "
    "reusable or creatable duration. A rounded type may need creation too. "
    "Examples: missing 30 creates 30 rather than using 40; missing 110 creates 120 even if "
    "only 150 exists. Pass the original user duration and maximum_minutes, capped by any explicit "
    "end interval. Respect selected types; clarify before switching. "
    "Creation remains subject to schedule/API validation.",
    "create_booking": "Book after live validation. Provide attendee and required fields. "
    "Follow plan_booking_duration; create missing types first. Keep duration_minutes equal to "
    "the original request, including when the selected type uses a rounded duration.",
    "reschedule_booking": "Move exact booking preserving duration; validate live slots first.",
}


def definitions():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DESCRIPTIONS.get(name, name.replace("_", " ")),
                "parameters": schema.model_json_schema(),
            },
        }
        for name, schema in SCHEMAS.items()
    ]
