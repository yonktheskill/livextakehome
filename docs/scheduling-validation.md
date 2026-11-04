# Scheduling tool validation

Event-type creation reads the account's default schedule and `GET /v2/conferencing/default`.
It maps only exact known slugs: daily-video/cal-video → cal-video, google-meet → google-meet,
zoom → zoom, and office365-video → office365-video. For external providers it also reads
`GET /v2/conferencing` and requires exactly one matching, explicitly valid credential.
When a successful response has no saved default (omitted data, null, or an empty object),
creation uses built-in Cal Video, matching Cal.com's own default-location behavior.
Malformed or unknown explicit defaults and missing, invalid, or ambiguous external accounts
still require clarification/account correction; request failures are not treated as defaults.
The creation payload explicitly supplies `locations`, and confirmation/receipt copy uses the
same resolved provider.
These reads use the authenticated connection; tests use mocks, never live accounts.

Booking answers accept bounded strings, finite numbers, booleans, and string arrays:
50 fields, 200 characters per key, 4,000 characters per string, 50 array entries,
and numeric magnitude at most 10^15.
Before checking slots, live booking field definitions determine required values, answer
types, and exact select/radio/multiselect/checkbox options. Required strings and arrays
must be nonempty; boolean false and numeric zero remain valid answers. Unknown fields,
unsupported definition types, conflicting identity values, and invalid email/URL/international
phone formats fail closed. Name/email use `attendee`; attendeePhoneNumber maps to
`attendee.phoneNumber`; guests maps to top-level `guests`; location uses the separately
validated exact offered location. Other validated answers use `bookingFieldsResponses`.
Structured address/split-name answers and attendee-defined locations still require Cal.com.
Phone location selection matches an offered phone value, requiring one exact match.

`list_bookings` accepts `cursor` and `max_pages` (1–5, default 5). Each call reads at most
five pages of 100 bookings. Results retain local interval overlap filtering, so appointments
crossing the requested boundaries remain visible. No API date filter excludes such meetings.
When truncated, `next_cursor` resumes the search with identical start/end/status; empty partial
results explicitly do not establish absence. Continue only as needed for the user's request;
never automatically loop indefinitely. Repeated/malformed cursors and hasMore without a cursor
produce an incomplete-search error. API cursors are opaque and reflect upstream pagination,
not a snapshot guarantee if bookings change during a search.

Preferred-name saving requires one explicit self-identification clause matching the proposed
name, such as “call me Bob” or “my name is Bob”. An attendee's name elsewhere in the message
is insufficient. Multiple identities, negation, alternatives, quotations, or questions require
clarification. The deterministic recognizer deliberately accepts a narrow set of English
self-identification phrases; unsupported wording requires clarification.

## Official contract references

- [Default conferencing app](https://cal.com/docs/api-reference/v2/conferencing/get-your-default-conferencing-application): appSlug/appLink response.
- [Cal.com default locations](https://github.com/calcom/cal.com/blob/main/packages/app-store/_utils/getDefaultLocations.ts): built-in Cal Video fallback when no default is saved.
- [Connected conferencing apps](https://cal.com/docs/api-reference/v2/conferencing/list-your-conferencing-applications): credential type and invalid flag.
- [Create event type](https://cal.com/docs/api-reference/v2/event-types/create-an-event-type): explicit integration locations; omission defaults to Cal Video.
- [Create booking](https://cal.com/docs/api-reference/v2/bookings/create-a-booking): attendee.phoneNumber, guests, location and bookingFieldsResponses.
- [Booking field definitions](https://github.com/calcom/cal.com/blob/main/packages/platform/types/event-types/event-types_2024_06_14/inputs/booking-fields.input.ts): boolean versus checkbox-group and selection options.

## Offline verification

`cd backend && uv run --offline pytest -q`

`cd backend && uv run --offline ruff check app/tools tests/test_scheduling_validation.py tests/test_tools.py tests/test_duration_policy.py`

`cd backend && uv run --offline mypy app/tools`

Focused regressions cover exact provider payload/confirmation, Cal Video fallback for successful
empty defaults, malformed/unsupported defaults and missing/invalid/ambiguous external accounts,
typed answers and standard mappings, nonempty required fields and option validation,
phone-location ambiguity, continuation beyond 500 bookings with crossing intervals, pagination
failures, and mistaken attendee-name persistence.
