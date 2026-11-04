"""Narrow Cal.com HTTP transport. No raw bodies/exceptions escape to model or browser."""

import ssl

import httpx

from app.core.config import Settings

VERSIONS = {
    "bookings": "2026-02-25",
    "bookings_list": "2026-05-01",
    "event-types": "2026-06-12",
    "slots": "2024-09-04",
    "schedules": "2024-06-11",
}


class CalError(Exception):
    def __init__(self, message, *, uncertain=False, code="calcom_error", diagnostics=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.code = code
        self.diagnostics = diagnostics or {}


class CalClient:
    def __init__(self, key: str, transport=None, *, settings: Settings | None = None):
        # Honor configured CA roots without enabling ambient HTTP(S) proxies.
        settings = settings if settings is not None else Settings()
        cafile, capath = settings.ssl_cert_file, settings.ssl_cert_dir
        verify = (
            ssl.create_default_context(cafile=cafile, capath=capath) if cafile or capath else True
        )
        self.http = httpx.AsyncClient(
            base_url="https://api.cal.com",
            timeout=25,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            verify=verify,
        )

    async def request(self, method, path, *, params=None, payload=None, version=None):
        resource = path.split("/")[2]
        version = version or VERSIONS.get(
            "bookings_list" if path == "/v2/bookings" and method == "GET" else resource
        )
        headers = {"cal-api-version": version} if version else {}
        try:
            response = await self.http.request(
                method, path, params=params, json=payload, headers=headers
            )
        except httpx.RequestError as exc:
            if isinstance(exc, httpx.ConnectError):
                message = (
                    "Could not connect to Cal.com. "
                    "Check backend network access and trusted TLS CA certificates."
                )
            elif isinstance(exc, httpx.TimeoutException):
                message = "Cal.com request timed out; try again later."
            else:
                message = "Cal.com connection failed; check backend network access."
            uncertain = method != "GET"
            if uncertain:
                message += " The operation outcome may be unknown."
            raise CalError(
                message,
                uncertain=uncertain,
                code="calcom_transport",
                diagnostics={"phase": "calcom_request", "cause_class": type(exc).__name__},
            ) from None
        if response.status_code >= 400:
            messages = {
                401: "Cal.com key expired or invalid; reconnect in settings.",
                403: "Cal.com denied this operation.",
                409: "Time or slug conflict.",
                429: "Cal.com rate limit reached; try again later.",
            }
            raise CalError(
                messages.get(response.status_code, "Cal.com rejected this request."),
                code="calcom_http",
                diagnostics={"phase": "calcom_response", "http_status": response.status_code},
                uncertain=method != "GET"
                and (response.status_code >= 500 or response.status_code == 408),
            )
        try:
            body = response.json()
            if body.get("status") == "error":
                raise CalError("Cal.com reported an error.", uncertain=method != "GET")
            if "pagination" in body:
                return {"bookings": body.get("data", []), "pagination": body["pagination"]}
            return body.get("data", body)
        except (ValueError, AttributeError):
            raise CalError("Invalid Cal.com response.", uncertain=method != "GET") from None

    async def close(self):
        await self.http.aclose()


def owned_booking(data, owner, *, allow_seated=False):
    hosts = data.get("hosts", [])
    if not any(str(h.get("id")) == str(owner) for h in hosts):
        raise CalError("Booking ownership could not be verified.")
    if data.get("recurringBookingUid") or data.get("recurrence"):
        raise CalError("Recurring bookings require the Cal.com interface.")
    seated = data.get("seats") or any(a.get("seatUid") for a in data.get("attendees", []))
    if seated and not allow_seated:
        raise CalError("Seated booking rescheduling requires the Cal.com interface.")
    return data


def owned_type(data, owner):
    users = data.get("users", [])
    owner_id = data.get("ownerId", data.get("userId"))
    matches = (
        (str(owner_id) == str(owner))
        if owner_id is not None
        else (len(users) == 1 and str(users[0].get("id")) == str(owner))
    )
    if data.get("teamId") or not matches:
        raise CalError("Personal event-type ownership could not be verified.")
    return data
