"""Defaults for descriptive metadata only; scheduling facts stay user/live-owned."""

ACTION_REASONS = {
    "cancel_booking": "User requested cancellation",
    "reschedule_booking": "User requested rescheduling",
}


def blank(value):
    # False and zero are meaningful answers. Never filter on truthiness alone.
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def optional_text(value):
    # Canonicalize absent text consistently for exact authorization and deduplication.
    # Preserve malformed nonstrings for schema rejection rather than defaulting them.
    return "" if value is None or (isinstance(value, str) and not value.strip()) else value


def action_reason(name, value):
    return ACTION_REASONS[name] if blank(value) else value


def booking_title(event_type):
    title = event_type.get("title")
    return title if isinstance(title, str) and title.strip() else "Meeting"


def sole_location(locations):
    if len(locations) != 1:
        return None
    location = locations[0]
    key = {
        "integration": "integration",
        "address": "address",
        "link": "link",
        "phone": "phone",
    }.get(location.get("type"))
    value = location.get(key) if key else None
    return location if isinstance(value, str) and value.strip() else None
