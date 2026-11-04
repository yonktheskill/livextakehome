from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.tools.defaults import optional_text

DURATION_DESCRIPTION = (
    "Exact duration currently agreed with the user; do not change without authorization."
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteArguments(Arguments):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    request_revision: int | None = Field(default=None, ge=1)


class SavePreferredName(WriteArguments):
    source_message_id: str | None = Field(default=None, min_length=1, max_length=200)
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
    cursor: str | None = Field(default=None, min_length=1, max_length=2000)
    max_pages: int = Field(default=5, ge=1, le=5)
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

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip():
            raise ValueError("Provide the attendee's name")
        return value

    @field_validator("time_zone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Use an IANA time zone") from None
        return value


AnswerText = Annotated[StrictStr, Field(max_length=4000)]
BookingAnswer = (
    None
    | AnswerText
    | StrictBool
    | Annotated[StrictInt, Field(ge=-1e15, le=1e15)]
    | Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-1e15, le=1e15)]
    | Annotated[list[AnswerText], Field(max_length=50)]
)


class CreateBooking(EventType, WriteArguments):
    start: datetime
    attendee: Attendee
    duration_minutes: int | None = Field(
        default=None, gt=0, le=1440, description=DURATION_DESCRIPTION
    )
    explicit_end: datetime | None = None
    maximum_minutes: int | None = Field(default=None, gt=0)
    booking_fields: dict[Annotated[str, Field(min_length=1, max_length=200)], BookingAnswer] = (
        Field(default_factory=dict, max_length=50)
    )
    location: str | None = Field(default=None, max_length=1000)

    @field_validator("start")
    @classmethod
    def aware_start(cls, value):
        return Range.aware(value)

    @field_validator("explicit_end")
    @classmethod
    def aware_end(cls, value):
        return Range.aware(value) if value is not None else None


class ReasonArguments(WriteArguments):
    reason: str | None = Field(
        default="",
        max_length=500,
        description="Optional user-supplied reason. The backend uses a neutral "
        "user-requested action reason when omitted, null or blank.",
    )

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value):
        return optional_text(value)


class Reschedule(Booking, ReasonArguments):
    new_start: datetime

    @field_validator("new_start")
    @classmethod
    def aware_start(cls, value):
        return Range.aware(value)


class Cancel(Booking, ReasonArguments):
    pass


class CreateType(WriteArguments):
    title: str = Field(min_length=1, max_length=120)
    duration_minutes: int = Field(gt=0, le=1440)
    requested_duration_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1440,
        description="Original booking duration when creating a missing type.",
    )
    description: str | None = Field(default="", max_length=1000)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value):
        if not value.strip():
            raise ValueError("Provide a reusable event-type title")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value):
        return optional_text(value)


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
    "list_bookings": "List owned bookings overlapping start/end, including crossing appointments. "
    "Reads at most max_pages (1–5) global pages. If truncated, pass next_cursor as cursor "
    "with identical dates/status to continue. Empty truncated pages do not mean no bookings. "
    "Do not claim completeness until truncated is false.",
    "save_preferred_name": "Save only the authenticated user's explicitly supplied name. "
    "Earlier self-identification may be referenced with source_message_id, an original user "
    "message from this conversation at or before the current message. Never infer from email "
    "or attendees. Works without a Cal.com connection.",
    "cancel_booking": "Cancel an exact owned whole booking when clearly user-authorized. "
    "Ambiguous target or scope requires focused clarification; no repeated permission after "
    "clear authorization. All attendees and seats are included.",
    "create_event_type": "Create a reusable personal type using the live default schedule "
    "and default conferencing app (built-in Cal Video when no default is saved). "
    "Implicit creation requires confirmation; clear "
    "explicit creation can be authorized, including compound requests. Accept positive minute "
    "lengths; no fixed UI whitelist. Preserve the original requested_duration_minutes when "
    "creating a missing booking type. Creation alone does not book an appointment. API errors "
    "do not authorize different lengths or persistent configuration.",
    "plan_booking_duration": "Optional helper to find exact live reuse or propose exact-length "
    "creation. Pass suitable IDs and the original duration and maximum. A missing reusable "
    "type still needs creation permission. Never silently round up.",
    "create_booking": "Book after live ownership, configuration, fields and availability "
    "validation. Provide the exact user-authorized duration and attendee details. Lookup "
    "sequence and use of the duration helper are optional. Blank optional answers are "
    "omitted. Missing standard booking title uses the event type title or Meeting. "
    "Required custom answers still need clarification. request_id associates a write "
    "with its durable user request when available; it is never sent to Cal.com.",
    "reschedule_booking": "Move exact booking preserving duration; validate live slots first.",
}


def definitions(*, voice=False):
    from app.db.requests import request_tool_definitions
    from app.tools.authorization import authorization_tool_definitions

    return (
        [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": DESCRIPTIONS.get(name, name.replace("_", " ")),
                    "parameters": compact_schema(schema.model_json_schema()),
                },
            }
            for name, schema in SCHEMAS.items()
        ]
        + request_tool_definitions()
        + authorization_tool_definitions()
        + (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "end_call",
                        "description": "End voice when the caller asks to hang up, "
                        "stop talking, or says they are done. This never cancels "
                        "a booking. Do not call while the caller still wants help. "
                        "The application handles the farewell and disconnects after playback.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            ]
            if voice
            else []
        )
    )


def compact_schema(value):
    """Discard generated display titles; retain all actual validation constraints."""
    if isinstance(value, dict):
        return {
            k: compact_schema(v)
            for k, v in value.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(value, list):
        return [compact_schema(v) for v in value]
    return value
