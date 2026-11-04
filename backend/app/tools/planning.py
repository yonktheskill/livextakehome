"""Live reads and narrow payload assembly, completed before dispatch is claimed."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import AnyHttpUrl, EmailStr, TypeAdapter, ValidationError

from app.calcom.client import CalError, owned_booking, owned_type
from app.tools.durations import duration, duration_plan


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def get_type(client, type_id, owner):
    return owned_type(await client.request("GET", f"/v2/event-types/{type_id}"), owner)


async def plan_booking_duration(client, args, owner):
    # The agent selects semantically suitable types, but IDs and lengths come from live reads.
    event_types = [
        await get_type(client, type_id, owner) for type_id in dict.fromkeys(args.event_type_ids)
    ]
    return duration_plan(event_types, args.duration_minutes, args.maximum_minutes)


async def get_booking(client, uid, owner, *, allow_seated=False):
    booking = owned_booking(
        await client.request("GET", f"/v2/bookings/{uid}"), owner, allow_seated=allow_seated
    )
    if booking.get("uid") != uid:
        raise CalError("Booking reference does not match the requested target.")
    return booking


async def slots(client, type_id, start, end, minutes, uid=None):
    params = {"eventTypeId": type_id, "start": iso(start), "end": iso(end), "timeZone": "UTC"}
    if minutes:
        params["duration"] = minutes
    if uid:
        params["bookingUidToReschedule"] = uid
    return await client.request("GET", "/v2/slots", params=params)


def slot_exists(data, start):
    values = [s for day in data.values() if isinstance(day, list) for s in day]
    for slot in values:
        value = slot.get("start", slot.get("time")) if isinstance(slot, dict) else slot
        if isinstance(value, str):
            try:
                if datetime.fromisoformat(value.replace("Z", "+00:00")) == start:
                    return True
            except ValueError:
                pass
    return False


async def default_location(client):
    default = await client.request("GET", "/v2/conferencing/default")
    slug = default.get("appSlug") if isinstance(default, dict) else None
    # Cal.com uses built-in Cal Video when no account default is saved. The API
    # may omit data entirely, leaving the success envelope after client unwrapping.
    if default is None or default in ({}, {"status": "success"}):
        slug = "cal-video"
    supported = {
        "daily-video": ("cal-video", "Cal Video", None),
        "cal-video": ("cal-video", "Cal Video", None),
        "google-meet": ("google-meet", "Google Meet", "google_video"),
        "zoom": ("zoom", "Zoom", "zoom_video"),
        "office365-video": ("office365-video", "Microsoft Teams", "office365_video"),
    }
    if not isinstance(slug, str) or slug not in supported:
        raise CalError("Ask the user to set one supported default conferencing app in Cal.com.")
    integration, label, credential_type = supported[slug]
    if credential_type:
        accounts = await client.request("GET", "/v2/conferencing")
        matches = [a for a in accounts if a.get("type") == credential_type]
        if len(matches) != 1 or matches[0].get("invalid") is not False:
            raise CalError(
                "Default conferencing account is missing, invalid or ambiguous; "
                "ask the user to resolve it in Cal.com first."
            )
    return {"type": "integration", "integration": integration}, label


def booking_answers(event_type, args):
    """Validate live definitions; map standard answers to dedicated API fields."""
    definitions = {}
    for field in event_type.get("bookingFields", []):
        slug = field.get("slug")
        if not slug or slug in definitions:
            raise CalError("Booking field definitions are incomplete or ambiguous; use Cal.com.")
        definitions[slug] = field
    answers = dict(args.booking_fields)
    standard = {"name": args.attendee.name, "email": str(args.attendee.email)}
    if args.location:
        standard["location"] = args.location
    for slug, value in answers.items():
        if slug not in definitions:
            raise CalError(f"Unknown booking field: {slug}; use the live definitions.")
        if slug in standard and value != standard[slug]:
            raise CalError(f"Booking field {slug} conflicts with the attendee.")
    for slug, field in definitions.items():
        value = standard.get(slug, answers.get(slug))
        empty = value is None or value == [] or (isinstance(value, str) and not value.strip())
        if field.get("required") and empty:
            raise CalError(f"Missing nonempty required booking field: {slug}")
        if value is None:
            continue
        kind = field.get("type", field.get("field"))
        valid = False
        if kind in {
            "text",
            "textarea",
            "name",
            "email",
            "phone",
            "address",
            "url",
            "select",
            "radio",
        }:
            valid = isinstance(value, str) and bool(value.strip())
        elif kind in {"multiselect", "multiSelect", "checkbox", "multiemail"} or slug == "guests":
            valid = isinstance(value, list) and all(v.strip() for v in value)
        elif kind == "boolean":
            valid = isinstance(value, bool)
        elif kind == "number":
            valid = type(value) in {int, float}
        elif kind == "system" and slug in standard:
            valid = True
        if slug == "location" and value == args.location:
            valid = True
        if slug in {"notes", "title", "rescheduleReason"}:
            valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            raise CalError(f"Unsupported or invalid answer type for booking field: {slug}")
        try:
            if kind == "email":
                TypeAdapter(EmailStr).validate_python(value)
            elif kind == "multiemail":
                TypeAdapter(list[EmailStr]).validate_python(value)
            elif kind == "url":
                TypeAdapter(AnyHttpUrl).validate_python(value)
        except ValidationError:
            raise CalError(f"Invalid {kind} answer for booking field: {slug}") from None
        if kind == "phone" and not re.fullmatch(r"\+[1-9]\d{6,14}", str(value)):
            raise CalError(f"Provide an international phone number for booking field: {slug}")
        if kind in {"select", "radio", "multiselect", "multiSelect", "checkbox"}:
            options = field.get("options", [])
            allowed = [o.get("value") if isinstance(o, dict) else o for o in options]
            values = value if isinstance(value, list) else [value]
            if not allowed or any(v not in allowed for v in values):
                raise CalError(f"Choose live options for booking field: {slug}")
    guests = answers.pop("guests", None)
    if guests is not None:
        try:
            guests = TypeAdapter(list[EmailStr]).validate_python(guests)
        except ValidationError:
            raise CalError("Provide valid guest email addresses.") from None
    phone = answers.pop("attendeePhoneNumber", None)
    if phone is not None and not re.fullmatch(r"\+[1-9]\d{6,14}", str(phone)):
        raise CalError("Provide attendeePhoneNumber in international format.")
    for slug in standard:
        answers.pop(slug, None)
    return answers, phone, guests


async def prepare(client, name, args, owner):
    if name == "create_event_type":
        if (
            args.requested_duration_minutes is not None
            and args.duration_minutes != args.requested_duration_minutes
        ):
            raise CalError("Create the exact requested duration; clarify any changed length first.")
        schedules = await client.request("GET", "/v2/schedules")
        defaults = [s for s in schedules if s.get("isDefault")]
        if len(defaults) != 1:
            raise CalError("Set one default availability schedule in Cal.com first.")
        location, location_label = await default_location(client)
        slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-") or "meeting"
        payload = {
            "title": args.title,
            "slug": f"{slug[:70]}-{uuid.uuid4().hex[:8]}",
            "lengthInMinutes": args.duration_minutes,
            "description": args.description,
            "scheduleId": defaults[0]["id"],
            "locations": [location],
            "minimumBookingNotice": 60,
            "beforeEventBuffer": 0,
            "afterEventBuffer": 0,
        }
        return (
            "/v2/event-types",
            payload,
            {
                "title": args.title,
                "duration_minutes": args.duration_minutes,
                "requested_duration_minutes": args.requested_duration_minutes,
                "schedule_id": defaults[0]["id"],
                "location": location_label,
                "minimum_notice_minutes": 60,
            },
        )
    if name == "create_booking":
        event_type = await get_type(client, args.event_type_id, owner)
        minutes = duration(event_type, args.duration_minutes)
        if args.maximum_minutes and minutes > args.maximum_minutes:
            raise CalError("Supported duration exceeds your maximum; please clarify.")
        if args.explicit_end and args.start + timedelta(minutes=minutes) > args.explicit_end:
            raise CalError("Effective duration would exceed the explicit end; please clarify.")
        answers, phone, guests = booking_answers(event_type, args)
        locations = event_type.get("locations", [])
        if len(locations) > 1 and not args.location:
            raise CalError("Choose a location from this event type first.")
        selected_location = None
        if args.location:
            candidates = [
                x
                for x in locations
                if args.location
                in {
                    x.get("type"),
                    x.get("integration"),
                    x.get("address"),
                    x.get("link"),
                    x.get("phone"),
                }
            ]
            if len(candidates) != 1:
                raise CalError("Choose one exact location offered by this event type.")
            selected_location = {
                k: v
                for k, v in candidates[0].items()
                if k in {"type", "integration", "address", "link", "phone", "public"}
            }
            if selected_location.get("type") in {
                "attendeeAddress",
                "attendeePhone",
                "attendeeDefined",
            }:
                raise CalError("Attendee-defined locations require booking through Cal.com.")
        available = await slots(
            client, args.event_type_id, args.start, args.start + timedelta(days=1), minutes
        )
        if not slot_exists(available, args.start):
            raise CalError("Requested time is unavailable for the supported duration.")
        payload = {
            "eventTypeId": args.event_type_id,
            "start": iso(args.start),
            "attendee": {
                "name": args.attendee.name,
                "email": str(args.attendee.email),
                "timeZone": args.attendee.time_zone,
            },
        }
        if event_type.get("lengthInMinutesOptions"):
            payload["lengthInMinutes"] = minutes
        if guests is not None:
            payload["guests"] = guests
        if phone is not None:
            payload["attendee"]["phoneNumber"] = phone
        if answers:
            payload["bookingFieldsResponses"] = answers
        if args.location:
            payload["location"] = selected_location
        return (
            "/v2/bookings",
            payload,
            {
                "effective_duration_minutes": minutes,
                "requested_duration_minutes": args.duration_minutes,
                "start": iso(args.start),
            },
        )
    booking = await get_booking(
        client, args.booking_uid, owner, allow_seated=name == "cancel_booking"
    )
    if booking.get("status", "").lower() in {"cancelled", "rejected"}:
        raise CalError("Booking is already cancelled or rejected.")
    details = {k: booking.get(k) for k in ("uid", "title", "start", "end", "status", "attendees")}
    if name == "cancel_booking":
        # Bind confirmation to the whole live booking, including its seat configuration.
        details.update(cancellation_scope="whole_booking", seats=booking.get("seats"))
        return (
            f"/v2/bookings/{args.booking_uid}/cancel",
            {"cancellationReason": args.reason},
            details,
        )
    start = datetime.fromisoformat(booking["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(booking["end"].replace("Z", "+00:00"))
    minutes = int((end - start).total_seconds() / 60)
    type_id = booking.get("eventTypeId", (booking.get("eventType") or {}).get("id"))
    if not type_id:
        raise CalError("Booking event type is missing; cannot safely reschedule.")
    event_type = await get_type(client, type_id, owner)
    available = await slots(
        client,
        type_id,
        args.new_start,
        args.new_start + timedelta(days=1),
        minutes,
        args.booking_uid,
    )
    if not slot_exists(available, args.new_start):
        raise CalError("New time is unavailable for the existing duration.")
    return f"/v2/bookings/{args.booking_uid}/reschedule", {"start": iso(args.new_start)}, details
