"""Exact requested lengths; live event-type options govern reuse.

Cal.com's create-event-type API accepts lengthInMinutes >= 1, not a UI whitelist.
The schemas retain the application's 24-hour bound for scheduling requests.
"""

from app.calcom.client import CalError


def supported_durations(event_type):
    default = event_type.get("lengthInMinutes", event_type.get("length"))
    choices = event_type.get("lengthInMinutesOptions") or [default]
    return sorted({x for x in choices if type(x) is int and x > 0})


def duration_plan(event_types, requested, maximum=None):
    """Optional helper: reuse the exact length or propose exact-length creation."""
    if type(requested) is not int or not 0 < requested <= 1440:
        raise CalError("Provide a positive duration of at most 1440 minutes.")
    if maximum is not None and requested > maximum:
        raise CalError("Requested duration exceeds the maximum; clarify.")
    ids = [t["id"] for t in event_types if requested in supported_durations(t)]
    return {
        "action": "reuse" if ids else "create_event_type",
        "requested_duration_minutes": requested,
        "effective_duration_minutes": requested,
        "rounded": False,
        "event_type_ids": ids,
        "creation_requires_confirmation": not bool(ids),
    }


def duration(event_type, requested):
    default = event_type.get("lengthInMinutes", event_type.get("length"))
    requested = requested if requested is not None else default
    if type(requested) is not int or requested <= 0 or not supported_durations(event_type):
        raise CalError("Event type does not declare a supported duration.")
    if requested not in supported_durations(event_type):
        raise CalError(
            "This event type does not offer the requested duration. Select an exact live "
            "length, ask permission to create_event_type, or clarify an acceptable alternative. "
            "Do not silently lengthen the booking or infer alternatives from API errors."
        )
    return requested
