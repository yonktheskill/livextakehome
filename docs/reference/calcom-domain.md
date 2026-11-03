# Cal.com Domain Model and API Guide for an AI Scheduling Agent

> Last reviewed: 2026-11-03. Focus: Cal.com API v2, single-user scheduling MVP. This is an agent-facing reference, not an API schema. Verify endpoint-specific payloads, authorization, and version headers against the live docs before sending requests.

## 0. Critical rules for the agent

1. **Do not confuse `Event Type`, `Booking`, and `Cal Event`.** They represent different resources.
2. **For routine one-on-one appointments, use Bookings + Event Types + Schedules/Slots**, not the separate Cal Events product.
3. **An event type is a reusable booking template, not an appointment on a calendar.** Creating an event type does not reserve a time.
4. **A booking is an actual scheduled appointment** with start/end times, attendees, status, and a booking UID.
5. **Cal Events** (announced as a prelaunch in August 2026) is a separate event-hosting/RSVP/ticketing feature. Do not infer that its API follows the Bookings API.
6. **The word “event” alone is ambiguous.** Older Cal.com help pages sometimes call event types “events”; external calendars call their entries “events”; and the new Cal Events feature uses the same noun. Determine which resource the user means before mutating anything.
7. **Never assume arbitrary durations are accepted by the Bookings API.** Check the linked event type's configured permitted durations and returned available slots.
8. **Never silently overwrite or cancel bookings.** Identify the exact booking, check scope and conflicts, and confirm destructive actions with the user.
9. **Do not assume Cal.com is a replacement for a general-purpose calendar.** Its native booking records are distinct from Google/Outlook personal calendar events.
10. **Use live API responses as the authority** for IDs, booking UIDs, statuses, durations, and actual availability. Do not invent availability or bookings.

## 1. Domain glossary

| Term | What it is | Example | Creates/reserves time? | Agent usage |
|---|---|---|---|---|
| **Event Type** | Reusable rules/template for how an appointment can be booked: title, slug, default or selectable durations, hosts, location, schedule, limits, etc. | “Consultation”, 30 min | **No** | Select/configure before creating a routine booking |
| **Booking** | Concrete scheduled appointment under an event type (normal case), with start/end, host, attendee(s), status and UID. | Alice's consultation on Nov 5, 2:00–2:30 PM | **Yes**, subject to confirmation/status and conflict rules | List, inspect, create, reschedule, cancel |
| **Cal Event / Events** | Separate event-hosting feature for defined events with guest lists, RSVPs, tickets and optionally paid registration; introduced/prelaunched in v6.8. | A workshop with registrations and tickets | Not equivalent to a routine appointment booking | Only use for event hosting / registration when specifically requested |
| **External calendar event** | A calendar provider's event, e.g. Google Calendar event with its own provider ID. | “Dentist”, 4–5 PM, in Google Calendar | Blocks times when configured for conflict checking | Read/write using appropriate calendar integration/API |
| **Availability Schedule** | Regular hours and exceptions defining when a person may accept bookings, in a specified time zone. | Weekdays 9 AM–5 PM | **No** | Configure availability; it does not make appointments |
| **Slot** | A potential bookable start/time interval calculated from event type, schedule, restrictions, and conflicts. | Nov 5, 2 PM, available for a 30-min call | **No** | Query before creating or rescheduling |
| **Host** | The person/team member whose time is booked | The Cal.com account owner | N/A | Determines booking availability and ownership |
| **Attendee / Booker** | Person making or attending a booking; may require name, email, time zone | Alice | N/A | Supply required attendee data |
| **Booking UID** | Unique identifier for a specific booking | `some-booking-uid` | N/A | Use for read/reschedule/cancel; not interchangeable with event type ID |
| **Event type ID / slug** | Identifier or URL slug for a booking template | ID `123`, slug `consultation` | N/A | Use when creating booking / looking up slots |
| **Calendar connection** | Link to Google/Outlook/etc. for conflicts and calendar sync | Connected Google Calendar | N/A | Optional integration; not the same as a booking database |
| **Destination calendar** | Calendar where supported booked events are written | Work Google Calendar | N/A | Controls sync destination; verify configured integration |
| **Webhook** | Server notification of booking-related changes | `BOOKING_CREATED`-style event | No | Sync agent state; consult exact current event names and signatures |

### Mental model

```text
Availability Schedule (regular working hours)
                    \
Event Type (booking rules, allowed durations) ---> Slots (possible times)
                    /                                |
External calendar busy times (when connected)       | choose a slot
                                                     v
                                            Booking (real appointment)
                                                     |
                                   optional calendar integration/sync
                                                     v
                                         External calendar event

Cal Events (RSVPs / ticketing / hosted event registration)
    = distinct product/resource; not another name for a Booking.
```

**Important:** A slot is not guaranteed until creation succeeds; slot state can change between checking and booking. A pending booking may not have the same semantics as an accepted booking.

## 2. Event Types in detail

An **Event Type** is a repeatable configuration that tells Cal.com *how appointments of this kind are bookable*.

Potential settings (vary with event type and plan):
- Title, slug/booking URL, description
- Default duration and optional alternative/selectable durations
- Hosts: personal, collective, round robin, managed/team
- Availability schedule/time zone; start-time intervals
- Location: video call, phone, in person, custom location
- Booking questions, guests, approval/confirmation settings
- Minimum notice, buffers, frequency and future-booking limits
- Recurrence, seats, reminders/workflows

**One event type can generate many bookings.** Example: the single “30-minute consultation” event type could produce hundreds of distinct appointments over time. Changing the template is *not* the same as editing or rescheduling a particular booking.

### Duration values supplied by the application developer

The user observed the following event-type duration choices (in minutes):

```json
[5, 20, 25, 30, 40, 45, 50, 60, 75, 80, 90, 120, 150, 180, 240, 300, 360, 420, 480]
```

**Interpretation:** Treat this exact array as the application's **observed configured/UI choices**, *not* as a universal Cal.com API-wide duration limit. Other accounts, settings and API flows may offer different values. Notably, `15` is absent from the observed list although Cal.com's own documentation uses 15-minute examples.

**Agent policy for this integration:**
- When asked to configure an event type using the observed choices, prefer only these values unless configuration is fetched and indicates otherwise.
- When making a booking under a multiple-duration event type, `lengthInMinutes` may select a duration **that the event type actually allows**. Its presence does **not** give unrestricted arbitrary-length booking privileges.
- If `lengthInMinutes` is omitted, the event type's default length applies.
- Check available slots for the selected length before trying to book.
- If a user requests a duration not offered, explain the limitation or request approval to modify/create an event type; **do not silently round the duration**.
- For dynamic multi-host bookings without a stored event type, Cal.com's docs describe a **different** limited set of allowed lengths: `15, 30, 45, 60, 90` minutes. Do not conflate with the user-observed array.

Source: https://cal.com/docs/api-reference/v2/bookings/create-a-booking

### Do you need an event type to book?

**Normal bookings: yes.** A normal `POST /v2/bookings` identifies the event type via `eventTypeId`, or applicable event-type slug/owner fields.

**Narrow exception:** The create-booking documentation also defines a dynamic group booking using `usernames` for **2–20 distinct hosts**, without a stored event type. This is not a generic single-user, arbitrary calendar-event creation endpoint.

**For the agent MVP:** Create and reuse a generic event type or several meaningful ones, rather than creating a new event type per appointment unless the workflow requires it.

## 3. Bookings in detail

A **Booking** represents a scheduled occurrence. Typical conceptual fields include:

```json
{
  "uid": "booking-uid-from-api",
  "eventTypeId": 123,
  "title": "Consultation with Alice",
  "start": "2026-11-05T06:00:00Z",
  "end": "2026-11-05T06:30:00Z",
  "status": "accepted",
  "hosts": [{ "name": "Host" }],
  "attendees": [{ "name": "Alice", "email": "alice@example.com" }]
}
```

This is an **illustrative shape**, not a guaranteed complete endpoint response. Read actual fields and status values from the API.

### Booking lifecycle and commands

| User intent | Resource / action | API v2 |
|---|---|---|
| “What appointments do I have Friday?” | Query bookings, filter relevant date/status | `GET /v2/bookings` |
| “Show details of that meeting.” | Read a specific booking by UID | `GET /v2/bookings/{bookingUid}` |
| “When is there a free time?” | Query computed availability slots for event type | Slots API (see docs) |
| “Book Alice at 2 PM.” | Create a booking referencing an event type and attendee | `POST /v2/bookings` |
| “Move Alice to 4 PM.” | Reschedule specific booking UID | `POST /v2/bookings/{bookingUid}/reschedule` |
| “Cancel Alice's meeting.” | Cancel specific booking UID | `POST /v2/bookings/{bookingUid}/cancel` |
| “Change the standard 30-min appointment to 45 min.” | Modify event-type configuration | Event Types API, **not** booking reschedule |
| “Set my working hours to 10–6.” | Modify schedule | Schedules API, **not** booking creation |
| “Create a ticketed workshop.” | Cal Events feature | Confirm Events API support before implementation |

**Cancel ≠ hard delete.** Treat cancellation as a status/lifecycle change and preserve the returned record/history where available. Do not promise permanent erasure.

**Reschedule ≠ edit event type.** A reschedule moves an identified booking, not all appointments of that kind. Check response and refresh stored UID/state if the API reports replacements or related records.

### Sample normal booking request (illustrative)

```http
POST https://api.cal.com/v2/bookings
Authorization: Bearer <YOUR_API_KEY>
cal-api-version: <VERSION_REQUIRED_BY_CURRENT_CREATE_ENDPOINT>
Content-Type: application/json

{
  "start": "2026-11-05T06:00:00Z",
  "eventTypeId": 123,
  "attendee": {
    "name": "Alice",
    "email": "alice@example.com",
    "timeZone": "Asia/Singapore"
  }
}
```

- This example means **Nov 5, 2026, 2 PM Singapore time**.
- `123`, attendee details, and credentials are placeholders.
- Include `lengthInMinutes` only when appropriate for an event type configured to allow multiple durations.
- Honor current API version headers and auth requirements. The docs show a broad example containing mutually alternative fields; **do not copy all example fields together**.

## 4. The newer **Cal Events** product — not the same as Event Types

In Cal.com v6.8 (Aug 15, 2026), Cal.com announced a **prelaunch of Cal.com Events**, positioned as a way to **host an event**, with:

- Guest lists / guest management
- RSVPs / registrations
- Ticket selling and pricing
- Optional payments, seat holds, ticket confirmations and refunds

Example: A 100-person paid seminar with an RSVP/ticket flow.

**Compared with the standard scheduling model:**

| Question | Event Type → Booking | Cal Event |
|---|---|---|
| Main purpose | Arrange available appointment times | Host and register guests for a defined event |
| Reusable scheduling template? | Yes (Event Type) | Not the same concept |
| Individual appointment instance? | Yes (Booking) | No, it's the hosted event resource |
| RSVP / ticketing / guestlist central? | Not central to basic one-to-one booking | Yes |
| API to call for ordinary meeting management? | Bookings API | Do not assume Events API can substitute |

**API caution:** The current Cal.com API navigation shows a distinct **Events** category, but this guide does not assert specific Events REST paths or CRUD semantics without checking the live endpoint descriptions. Confirm account access, feature availability, and current API support before exposing `createCalEvent` or `manageRSVP` tools.

Source: https://cal.com/blog/calcom-v6-8

### Other ambiguous uses of “event”

- Older UI/docs sometimes say “create an event” but direct users to **Event Types**, meaning a template.
- “Google Calendar event” means a provider-side time entry, **not** a Cal.com booking object (though a booking may synchronize into one).
- “Booking event” in a webhook may mean **an occurrence or notification** (e.g., created/cancelled), not a standalone Cal Event.

**Agent disambiguation rule:** Map “make an appointment / schedule a call / cancel my meeting” to **Bookings**. Map “set up a bookable 30-min consultation link” to **Event Types**. Map “host a concert / sell tickets / accept RSVPs” to **Cal Events**. Map “add a personal reminder to my Google Calendar” to the **calendar provider's API**.

## 5. Availability, slots and calendars

**Availability Schedule** = when bookings *may* be accepted. It is not a list of meetings.

**Slots** = computed proposed times for a particular event type and possibly requested duration. Availability calculation may consider working hours, busy connected calendars, booking conflicts, buffers, booking limits, notice periods, etc.

**Connected calendar** = external provider (Google/Outlook/etc.) used for busy-time checks and/or to write booking copies. **Destination calendar** is where calendar events are written when sync is configured. A **calendar event** may exist without being a Cal.com booking, and vice versa.

**Without an external connection:** Cal.com has its own booking records and availability configuration, but it will not know unrelated provider-side appointments. Do not assume an unconnected calendar is always equivalent to a complete personal calendar. Test the exact no-external-calendar configuration and double-booking behavior in your environment.

**Important distinction:** A user saying “block two hours for studying” may want a generic calendar hold, *not* a customer-facing appointment. Do not force this through Cal.com Bookings unless that is an explicitly chosen application policy; a direct calendar API or separate internal calendar data model may fit better.

## 6. Suggested agent tools (MVP)

Expose narrowly scoped backend functions; the LLM should not assemble unrestricted API requests or hold Cal.com credentials.

```ts
listBookings({ from, to, status? })
getBooking({ bookingUid })
listEventTypes()
getAvailableSlots({ eventTypeId, dateFrom, dateTo, durationMinutes? })
createBooking({ eventTypeId, start, attendee, durationMinutes? })
rescheduleBooking({ bookingUid, newStart })
cancelBooking({ bookingUid, reason? })
```

Optional administration tools:

```ts
getAvailabilitySchedules()
updateAvailabilitySchedule(...)
createEventType(...)
updateEventType(...)
```

Separate (not MVP): `createCalEvent`, `listCalEvents`, `manageEventRSVPs` — only after the Cal Events API is investigated.

### Safe action procedure

1. Parse user intent; classify the resource (`booking`, `event type`, `Cal Event`, `availability schedule`, `external calendar event`).
2. Resolve user time zone and convert to ISO timestamps with offsets/UTC; handle ambiguous dates and DST.
3. For read: use the appropriate list/get endpoint and apply date/status filters; do not hallucinate records.
4. For create: resolve correct event type, validate attendee fields and duration, query slots, seek user confirmation if required by product policy, then create and re-read result.
5. For reschedule/cancel: retrieve candidate booking(s), disambiguate by attendee/title/date/UID, show exact target, confirm, mutate, then verify final status.
6. On a 409/validation/availability failure: do not claim success; fetch updated slots and propose alternatives.
7. Never automatically use bypass flags such as `allowConflicts`, `allowBookingOutOfBounds` or `skipBookingLimits` without an explicit trusted product rule and appropriate permissions.
8. Keep credentials server-side; authorize user access; verify webhook authenticity; use idempotency/deduplication where possible; log changes and tool outcomes.

## 7. Frequently misunderstood questions

**Q: If I create one event type, do I have to create another for each appointment?**
A: No. Reuse the existing type; create many individual bookings against it.

**Q: Does creating an event type put an appointment on my calendar?**
A: No. The event type is only a template.

**Q: Is the “Events” feature just a list of my appointments?**
A: No. The newer Cal Events feature is for event hosting/RSVP/ticketing. For regular appointments, query Bookings.

**Q: Can I create a booking without an event type?**
A: Normal single-user appointments require an event type. A documented narrow exception exists for dynamic group bookings with 2–20 hosts.

**Q: Can `lengthInMinutes` make any arbitrary duration?**
A: No. For a normal booking it selects from an event type's configured allowed lengths; the user-observed list is not a universal API guarantee.

**Q: Does Cal.com need Google Calendar to hold bookings?**
A: Cal.com has booking and availability resources of its own; external calendar connectivity adds conflict visibility and synchronization. Verify the precise calendar-free setup rather than asserting a full native general-purpose calendar.

**Q: Can I delete a booking?**
A: Use the cancellation endpoint for normal lifecycle handling; do not imply permanent deletion.

**Q: Can I edit an existing booking's duration or title arbitrarily?**
A: Not by assuming the API works like Google Calendar. Inspect the current Booking endpoints and the event-type constraints; use reschedule for supported time changes and avoid promising an arbitrary patch operation.

## 8. Primary official sources

- API v2 docs: https://cal.com/docs/api-reference/v2
- Create booking (including `lengthInMinutes` and dynamic group exception): https://cal.com/docs/api-reference/v2/bookings/create-a-booking
- List bookings: https://cal.com/docs/api-reference/v2/bookings/get-all-bookings
- Event type overview: https://cal.com/blog/event-types-guide-calcom
- Availability schedules: https://cal.com/blog/using-custom-availability-scheduling
- New Cal Events feature (Aug 2026): https://cal.com/blog/calcom-v6-8
- Current pricing/features: https://cal.com/pricing

---

**Implementation note:** Terminology and feature access may change. This file distinguishes confirmed product concepts from operational assumptions. Before implementing any action, check current API documentation and test the account's real permissions and configuration.
