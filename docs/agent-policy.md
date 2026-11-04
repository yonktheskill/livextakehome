# Agent autonomy and booking corrections

Complete clear scheduling requests autonomously. Ask a focused question when missing
information or uncertainty could change the operation, target, scope, attendees, time,
duration or persistent configuration. Clear instructions and approvals authorize their
specific action. Reusable template creation during booking needs permission unless it
was already authorized. Ownership, live availability, exact configuration, credentials,
Stop and unknown-write recovery remain enforced in Python.

## API defaults

Apply defaults to descriptive metadata before dispatch, preserving supplied nonempty
values. Cancellation and rescheduling use neutral "User requested cancellation" and
"User requested rescheduling" reasons when omitted, null or blank. These equivalent
empty reason forms normalize identically for exact authorization and deduplication.
A missing standard booking title uses the live event-type title, falling back to
"Meeting". The effective booking title is included in validation evidence.

Omit blank optional booking answers, empty guest lists, phone numbers and reusable-type
descriptions. Keep valid `false` and `0` answers. A sole configured host location can
satisfy a required location field; multiple choices and attendee-defined locations
require resolution. Live configuration still supplies durations, schedules and
conferencing defaults under the existing policy.

Missing identities, emails, dates, time zones, exact requested durations, resource IDs
and required custom answers are never filled with invented values. The agent clarifies
uncertainty before dispatch. Do not request optional metadata merely to satisfy the API,
or retry unknown writes with different defaults. These rules cover the supported tools;
they cannot remove availability, permission, account-configuration or provider failures.

## Independent work and corrections

Every original message enters a durable inbox. The ordinary model resolves messages
oldest first using `resolve_requests`, with explicit request IDs and revisions. It can
add independent requests, partially amend a unique request, hold ambiguous requests for
clarification, withdraw a request, explicitly resume blocked work, or dismiss a message
that adds no booking task. This is semantic interpretation, not a keyword classifier.
Voice turns can contain several accepted transcript segments sharing one agent run.
Every accepted segment is exposed and resolved in inbox order, including segments
without their own run. Audio timestamps do not determine that order. Unaccepted
partial, late or interrupted captions remain outside the actionable inbox.

A new message pauses stale execution without deleting independent work. Multiple clear
booking requests remain pending until each is completed or withdrawn. A correction
changes only stated fields of its identified task, including nested attendee details;
unchanged date, duration and attendee facts survive. Stored facts and original source
messages remain available through context compaction.

For “book this at noon instead,” an undispatched task books its corrected version once.
A dispatched action first settles. If it succeeded, the correction reschedules its actual
resulting UID, including replacement UID lineage. A definitive failure can be amended
and retried as corrected work. Unknown outcomes require reconciliation before another
mutation. Multiple possible referents or uncertainty between an additional booking and
a correction cause a clarification and hold affected tasks before dispatch.

Stop and recovery-blocked tasks require an explicit resume; normal follow-ups do not
silently revive them. Old database actions are backfilled conservatively at migration.
The transactional dispatch claim still defines the point after which a correction
cannot revoke an already committed external request.

## Authorization, durations and wording

`authorize_action` lets the model ground an exact cancellation or reusable-type creation
in an owned original user message when its wording requires semantic interpretation.
Evidence is bound to the current run and normalized exact arguments, persisted before
execution, and frozen with the outgoing request. External descriptions, assistant text,
future messages and other users' messages cannot supply this evidence. Unclear intent
requires a question, never an unconditional authorization flag.

Duration planning is optional. Preserve the exact agreed duration; actual live event-type
options govern reuse. Type creation no longer uses observed UI lengths as a whitelist.
The application retains its positive-minute/24-hour bound. Ask before changing duration
unless the user has already authorized that tolerance; respect explicit ends and maximums.

Credential filtering remains deterministic. Ordinary names such as Dick, harmless
frustration and references to instructions do not reject a scheduling request. Professional
responses and harmful-action refusal are model policy; word lists are not advertised as
semantic moderation or a complete prompt-injection defense.

## Runtime and validation

Context fitting compacts large list results before dropping complete tool exchanges.
Planner facts, IDs, mutation evidence and source continuations are preserved. Unchanged
cycles of up to four batches trigger one response-only recovery attempt within existing
step/time/tool caps. The agent cannot dispatch additional writes during that attempt.
A last-step action is reported as completed only from matching persisted success evidence.

Offline tests cover request state, race orderings, migrations, owner scope, dispatch
integration and scripted model tool decisions. These tests validate the protocol and
backend behavior; they do not constitute a live-model semantic accuracy evaluation or
live Cal.com account verification.
