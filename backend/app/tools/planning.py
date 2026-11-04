"""Live reads and narrow payload assembly, completed before dispatch is claimed."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from app.calcom.client import CalError, owned_booking, owned_type
from app.tools.durations import CREATABLE_DURATIONS, duration, duration_plan


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


async def get_booking(client, uid, owner):
    return owned_booking(await client.request("GET", f"/v2/bookings/{uid}"), owner)


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


async def prepare(client, name, args, owner):
    if name == "create_event_type":
        if args.requested_duration_minutes is not None:
            plan = duration_plan([], args.requested_duration_minutes)
            if args.duration_minutes != plan["effective_duration_minutes"]:
                raise CalError(
                    "Use plan_booking_duration: create the requested duration when allowed, "
                    "otherwise the smallest larger creation choice."
                )
        if args.duration_minutes not in CREATABLE_DURATIONS:
            plan = duration_plan([], args.duration_minutes)
            raise CalError(
                "Requested duration is outside the application's event-type creation choices. "
                f"The next creatable duration is {plan['effective_duration_minutes']} minutes; "
                "disclose the adjustment and propose that type instead."
            )
        schedules = await client.request("GET", "/v2/schedules")
        defaults = [s for s in schedules if s.get("isDefault")]
        if len(defaults) != 1:
            raise CalError("Set one default availability schedule in Cal.com first.")
        slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-") or "meeting"
        payload = {
            "title": args.title,
            "slug": f"{slug[:70]}-{uuid.uuid4().hex[:8]}",
            "lengthInMinutes": args.duration_minutes,
            "description": args.description,
            "scheduleId": defaults[0]["id"],
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
                "location": "Cal Video",
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
        required = {
            f["slug"]
            for f in event_type.get("bookingFields", [])
            if f.get("required") and f.get("slug") not in {"name", "email"}
        }
        if not required.issubset(args.booking_fields):
            raise CalError("Missing required booking fields: " + ", ".join(sorted(required)))
        locations = event_type.get("locations", [])
        if len(locations) > 1 and not args.location:
            raise CalError("Choose a location from this event type first.")
        selected_location = None
        if args.location:
            candidates = [
                x
                for x in locations
                if args.location
                in {x.get("type"), x.get("integration"), x.get("address"), x.get("link")}
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
        if args.booking_fields:
            payload["bookingFieldsResponses"] = args.booking_fields
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
    booking = await get_booking(client, args.booking_uid, owner)
    if booking.get("status", "").lower() in {"cancelled", "rejected"}:
        raise CalError("Booking is already cancelled or rejected.")
    details = {k: booking.get(k) for k in ("uid", "title", "start", "end", "status", "attendees")}
    if name == "cancel_booking":
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
