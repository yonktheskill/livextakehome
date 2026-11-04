"""Readable chat copy for confirmations and completed scheduling actions."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def meeting_time(value, time_zone):
    # Presentation must not roll back settlement of an already completed write.
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return str(value)
    try:
        zone = ZoneInfo(time_zone)
    except (ValueError, ZoneInfoNotFoundError):
        time_zone, zone = "UTC", ZoneInfo("UTC")
    date = (date if date.tzinfo else date.replace(tzinfo=UTC)).astimezone(zone)
    return f"{date:%A}, {date.day} {date:%B %Y at %H:%M} ({time_zone})"


def booking_details(details, time_zone):
    lines = []
    for field, label in (("start", "Starts"), ("end", "Ends")):
        if details.get(field):
            lines.append(f"- **{label}:** {meeting_time(details[field], time_zone)}")
    if details.get("uid"):
        lines.append(f"- **Booking reference:** {details['uid']}")
    return lines


def confirmation_message(name, details, time_zone):
    title = details.get("title") or "this meeting"
    if name == "cancel_booking":
        heading = f"Would you like me to cancel “{title}”?"
        lines = booking_details(details, time_zone)
        lines.append("- **Scope:** The whole booking, including all attendees and seats.")
        attendees = details.get("attendees") or []
        if attendees:
            labels = [
                " <".join(filter(None, (a.get("name"), a.get("email"))))
                + (">" if a.get("name") and a.get("email") else "")
                or "Unnamed attendee"
                for a in attendees
            ]
            lines.append(f"- **Attendees ({len(attendees)}):** " + "; ".join(labels))
    else:
        heading = f"Shall I create “{title}” as a reusable meeting type?"
        minutes = details["duration_minutes"]
        notice = details["minimum_notice_minutes"]
        lines = [
            f"- **Duration:** {minutes} minutes",
            f"- **Location:** {details['location']}",
            "- **Availability:** Your default schedule",
            f"- **Minimum notice:** {'1 hour' if notice == 60 else f'{notice} minutes'}",
        ]
        requested = details.get("requested_duration_minutes")
        if requested is not None and requested != minutes:
            heading += (
                f" You asked for {requested} minutes; "
                f"the next supported length is {minutes} minutes."
            )
    return "\n\n".join(
        [
            heading,
            "\n".join(lines),
            "Reply **yes** to confirm, or tell me what you’d like to change.",
        ]
    )


def action_message(kind, outcome, time_zone):
    validated = outcome.get("validated", {})
    response = outcome.get("response", {})
    details = {**validated, **response}
    title = details.get("title") or "your meeting"
    if kind == "create_event_type":
        minutes = validated.get("duration_minutes")
        duration = f"{minutes}-minute " if minutes else ""
        text = f"Created “{title}” as a reusable {duration}meeting type."
        if validated.get("location"):
            text += f" It uses {validated['location']}."
        return text
    status = response.get("status")
    if kind == "cancel_booking":
        text = f"Sent the cancellation request for “{title}”."
        if str(status).lower() in {"cancelled", "canceled"}:
            text = f"Cancelled “{title}”."
    else:
        text = f"Sent the reschedule request for “{title}”."
        if str(status).lower() in {"accepted", "confirmed"}:
            text = f"Rescheduled “{title}”."
    if status:
        text += f" Cal.com reports its status as **{status}**."
    # Reschedule validation describes the old time. Only the response can supply
    # the resulting appointment time; never present the old time as the new one.
    lines = booking_details(response, time_zone)
    return text + ("\n\n" + "\n".join(lines) if lines else "")
