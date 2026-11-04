"""Application duration policy; these creation choices are not a Cal.com API limit."""

from app.calcom.client import CalError

CREATABLE_DURATIONS = (
    5,
    20,
    25,
    30,
    40,
    45,
    50,
    60,
    75,
    80,
    90,
    120,
    150,
    180,
    240,
    300,
    360,
    420,
    480,
)


def supported_durations(event_type):
    default = event_type.get("lengthInMinutes", event_type.get("length"))
    choices = event_type.get("lengthInMinutesOptions") or [default]
    return sorted({x for x in choices if type(x) is int and x > 0})


def duration_plan(event_types, requested, maximum=None):
    """Exact reuse, exact creation, then the smallest reusable/creatable larger length."""
    existing: dict[int, list[int]] = {}
    for event_type in event_types:
        for minutes in supported_durations(event_type):
            existing.setdefault(minutes, []).append(event_type["id"])
    candidates = sorted(set(existing) | set(CREATABLE_DURATIONS))
    possible = [x for x in candidates if x >= requested and (maximum is None or x <= maximum)]
    if not possible:
        raise CalError("No reusable or creatable duration fits the request and maximum; clarify.")
    minutes = possible[0]
    ids = existing.get(minutes, [])
    return {
        "action": "reuse" if ids else "create_event_type",
        "requested_duration_minutes": requested,
        "effective_duration_minutes": minutes,
        "rounded": minutes != requested,
        "event_type_ids": ids,
        "creation_requires_confirmation": not bool(ids),
    }


def duration(event_type, requested):
    default = event_type.get("lengthInMinutes", event_type.get("length"))
    requested = requested if requested is not None else default
    if type(requested) is not int or requested <= 0 or not supported_durations(event_type):
        raise CalError("Event type does not declare a supported duration.")
    plan = duration_plan([event_type], requested)
    if plan["action"] != "reuse":
        minutes = plan["effective_duration_minutes"]
        raise CalError(
            f"This event type cannot satisfy the duration policy. Reuse another suitable "
            f"{minutes}-minute type or call create_event_type for {minutes} minutes "
            "before booking. "
            "Use plan_booking_duration and keep the user's original duration_minutes unchanged."
        )
    return plan["effective_duration_minutes"]
