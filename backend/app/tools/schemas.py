from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


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


class Slots(Range):
    event_type_id: int = Field(gt=0)
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
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
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
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
    description: str = Field(default="", max_length=1000)


SCHEMAS: dict[str, type[Arguments]] = {
    "save_preferred_name": SavePreferredName,
    "list_bookings": ListBookings,
    "get_booking": Booking,
    "list_event_types": Arguments,
    "get_event_type": EventType,
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
    "Implicit creation requires confirmation. Clear explicit creation can be authorized.",
    "create_booking": "Book after live validation. Provide attendee and required fields.",
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
