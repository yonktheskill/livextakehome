# Backend handoff

The ASGI entry point is `app.main:app`. Run one backend worker/replica. All
business endpoints retain `/api`; the browser contract is in `docs/API_CONTRACT.md`.
No commits were created by this implementation task.

## Local commands

From `backend/`:

```sh
uv sync --frozen
uv run uvicorn app.main:app --port 8000
uv run python -m app.maintenance migrate
uv run python -m app.maintenance cleanup-traces
uv run ruff check app tests
uv run ruff format --check app tests
uv run mypy app
uv run pytest -q
```

Configuration is environment-only, with no automatic dotenv read. Supply
`MASTER_SECRET` as an explicit Fernet deployment secret, `DATABASE_PATH` for the
persistent SQLite file, `PUBLIC_ORIGIN` for exact Origin matching and
`COOKIE_SECURE=true` behind production HTTPS. To create a *new* deployment secret
explicitly, run `uv run python -c 'from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())'` privately; never bake it into an image.
Do not rotate this value without retaining the previous decryption key. During
rotation, `MASTER_SECRET_PREVIOUS` is a JSON list of old Fernet keys; newly connected
keys use the current key. Existing rows can be reconnected while the old key is
available. Automatic bulk re-encryption is not implemented.

The configured default `LLM_PROVIDER=codex` currently fails at startup with explicit
guidance: the provider owner's isolation investigation disabled that adapter. This
is a known unavailable capability, not a working local inference mode. For offline
UI use set `LLM_PROVIDER=fake` explicitly. It labels its output as offline and does
not perform scheduling. For real inference select `LLM_PROVIDER=openrouter`,
`LLM_MODEL` and project `OPENROUTER_API_KEY` explicitly. No paid fallback occurs.
The optional `codex` dependency extra is pinned to the inspected SDK, but installing
it does not enable the unsafe runtime. See `docs/provider-notes.md`.

## Behaviour and safety

- Argon2id salted password hashes; opaque random sessions are stored only as SHA-256
  hashes. Cookies are HttpOnly and SameSite=Lax. Login rotates the browser session;
  logout revokes it. Origin and Fetch Metadata checks cover mutations. Auth and
  message endpoints have a bounded request rate per client/path per minute.
- Keys are validated with `/v2/me`, encrypted with authenticated user/owner binding,
  and never sent to providers. Different owner replacement requires DELETE then PUT.
  Key changes reject while an unresolved committed action exists, preserving the
  credential needed to investigate it. All run/history/replay lookups are user-scoped.
- Peewee 3.19 is locked and tested. Each complete synchronous repository unit runs
  in a worker thread with its own connection. SQLite WAL, FULL synchronous mode,
  foreign keys, busy timeout, and short IMMEDIATE write transactions are deliberate.
- Message dedupe, revisions, Stop, key changes and dispatch claims share the SQLite
  writer gate. Claims consume confirmation once. Mutations are serialized. Restart,
  transport errors, malformed successful mutation responses, and timeouts preserve
  unknown dispatch outcomes; unknown actions block further mutations, without resend.
- One supervised persisted queue consumer runs in lifespan, independent of SSE.
  Text chunks and durable run events commit together before exposure. Replay uses
  per-run sequence IDs, `after`, and Last-Event-ID. The runner bounds model steps,
  total tools, text length and total time. Detailed metadata traces expire after 30
  days; messages/actions/replay/context snapshots are retained.
- Exact cancellations are re-read and fingerprinted, expire after ten minutes, and
  accept only an unambiguous plain yes/confirm response to one pending proposal.
  Changed details require another confirmation. Recurring/seated bookings are excluded.
- Booking creation and rescheduling validate personal ownership and live slots.
  Duration rounds upward within the selected live type; explicit end/maximum remains
  a constraint. Rescheduling preserves duration and stores both original UID in inputs
  and replacement UID in canonical results. Required questions are collected before
  dispatch; arbitrary bypass flags cannot enter the schemas.
- Basic reusable types use the one default schedule and documented Cal Video default,
  60-minute notice, zero buffers and a unique generated slug. A narrowly matched explicit
  request such as “create a 45-minute Consultation type” executes directly. All other
  proposals ask exact permission, including implicit missing types. A combined
  create-and-book request currently needs the type confirmation before continuing.
- Inference uses bounded recent messages, extractive snapshots with original time/zone,
  canonical recent action evidence, and owner-scoped lexical history retrieval. It
  does not run a paid summarization call. Canonical history is not truncated/deleted.

## Limits requiring further work

This is substantial MVP infrastructure, not a claim of complete production readiness.
No live credentials, Cal.com mutations, paid model calls, Docker build, or real
provider scheduling evaluation were used in verification. The public Cal.com schemas
were checked, but account-specific permissions/integrations remain untested.

Unknown outcomes currently require operator investigation and explicit database
resolution after checking Cal.com; there is no reconciliation UI/CLI that can safely
infer ownership of an unknown newly created booking. The app deliberately remains
blocked rather than risking duplicate mutations. Bulk key re-encryption is deferred.

Listing walks at most five 100-record cursor pages and filters overlap locally to
include appointments crossing date boundaries; `truncated=true` means a partial view.
It may need more targeted/indexed lookup for large accounts. Team/recurring/seated,
verification-code and attendee-defined-location flows are outside this MVP. Complex
booking-field value types beyond text need extension. DST/ambiguous relative-date
clarification is prompted; Python requires offset-aware instants and valid IANA zones,
but does not independently parse natural-language time expressions.

The initial versioned schema is reviewed and idempotent; no historical schema upgrade
exists yet. Add sequential playhouse migrations when schema version 2 is introduced.
Only metadata (counts, elapsed time, provider/model, usage, correlation IDs) is traced;
raw prompts, raw upstream errors and credentials are excluded. This limits forensic
payload inspection intentionally. The debug endpoint is authenticated, scoped and
disabled unless `DEBUG_API=true`.

## Documentation verified without credentials

- Create event type: https://cal.com/docs/api-reference/v2/event-types/create-an-event-type
  (`2026-06-12`; positive length, title, slug; omitted location defaults to Cal Video).
- Schedules: https://cal.com/docs/api-reference/v2/schedules/get-all-schedules
  (`2024-06-11`, isDefault).
- Booking list: https://cal.com/docs/api-reference/v2/bookings/get-all-bookings
  (`2026-05-01`; limit, cursor, top-level pagination; single status; containment date filters).
- Booking create/reschedule and event-type reads were checked against their official
  endpoint pages. No Cal Events APIs are exposed.
