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

Codex is now an opt-in local development adapter with the pinned SDK. See
`docs/codex-development.md` and root `make setup-codex` / `make codex-login`.
The real runtime has an offline loopback test proving empty executable tools.
Live account inference remains untested. There is no automatic paid fallback.
Use explicit fake inference for offline UI checks, or configure OpenRouter model/key.

## Behaviour and safety

- Credential checks live in `app/core/guardrails.py`; ordinary names and harmless user
  wording are accepted. Complete replies/proposals are checked before exposure or
  execution. Professional output and harmful-action refusal are prompt policy, with
  code enforcing authenticated scope, schemas, live validation and dispatch evidence.
  There is no extra moderation service or keyword-based injection guarantee.

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
- Clear exact whole-booking cancellations do not require repeated permission. Semantic
  authorization can cite an owned original user message and freeze exact tool arguments
  through `authorize_action`; ambiguous targets/scopes still require clarification.
  Pending proposals bind to exposed canonical details and expire after ten minutes.
- Booking creation and rescheduling validate ownership, fields, locations and live slots.
  Duration planning is optional. Reuse the exact agreed live length or propose an exact
  new type; no UI-derived creation whitelist or silent rounding remains. Explicit ends
  and maximums remain binding. Rescheduling preserves attendees/duration and records UID
  replacements. Reusable types use the verified default schedule/conferencing app,
  60-minute notice, zero buffers and a unique slug. Implicit creation needs permission;
  explicit compound creation-and-booking instructions can authorize both operations.
- Schema version 5 adds durable booking requests, a semantic inbox and task-to-action
  revisions. Independent requests survive interruptions. `resolve_requests` classifies
  original messages oldest first; partial amendments preserve unstated details and
  completed bookings are rescheduled by their actual UID. Writes wait for pending
  interpretations, settling actions and reconciliation. Stop/restart never silently
  replays blocked historical requests. See `docs/agent-policy.md`.
- Inference uses bounded recent messages, extractive snapshots with original time/zone,
  canonical recent action evidence, and owner-scoped lexical history retrieval. Recent
  original user requests and clarifications have a protected reserve (up to 20 messages,
  bounded by `CONTEXT_INTENT_TOKENS`, default 1600, and one third of usable input). Tool
  results cannot evict that reserve and leave only a follow-up such as “retry”. It
  uses configurable estimated token budgets and a 70% compaction threshold, not
  just message counts; it does not run a paid summarization call. Canonical history is not truncated/deleted.
- History search defaults to user messages, ranks meaningful keywords, and falls back
  to recent requests when none match. Assistant replies require an explicit role filter.
  Excerpts are marked as truncated and remain retrievable; `source_id` fetches the
  original within the retrieval budget. Searches exclude later messages and the
  current run's own narration. Event-type lists omit unrelated API metadata; full
  records are re-read before writes. Independent model responses have paragraph
  separators in both canonical text and replay events; token fragments are unchanged.

## Limits requiring further work

This is substantial MVP infrastructure, not a claim of complete production readiness.
No live credentials, Cal.com mutations, paid model calls, Docker build, or real
provider scheduling evaluation were used in verification. The public Cal.com schemas
were checked, but account-specific permissions/integrations remain untested.

Unknown outcomes currently require operator investigation and explicit database
resolution after checking Cal.com; there is no reconciliation UI/CLI that can safely
infer ownership of an unknown newly created booking. The app remains blocked until a trusted operator verifies the frozen evidence
and uses the audited [reconciliation CLI](../docs/recovery.md); no request is resent. Bulk key re-encryption is deferred.

Listing walks at most five 100-record cursor pages and filters overlap locally to
include appointments crossing date boundaries; `truncated=true` means a partial view.
Pass `next_cursor` back as `cursor` with the same filters to continue beyond those pages.
Live booking fields accept validated bounded strings, booleans, finite numbers and string
arrays. See [scheduling validation](../docs/scheduling-validation.md). Team/recurring flows,
seated rescheduling, verification-code, structured address/split-name and attendee-defined
location flows are outside this MVP. DST/ambiguous relative-date
clarification is prompted; Python requires offset-aware instants and valid IANA zones,
but does not independently parse natural-language time expressions.

Schema version 2 adds unique logical dispatch identity and frozen request evidence.
Version 1 upgrades through Peewee migration APIs, invalidating legacy uncommitted
preparations that lack complete dispatch evidence. Idempotence/upgrade tests pass.
Detailed tool inputs/outputs/errors are now bounded and sanitized in traces;
model metadata includes counts, elapsed time, provider/model, usage and correlation IDs.
Raw prompts, private reasoning and credentials remain excluded. The debug endpoint is authenticated, scoped and
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

Review hardening: stale preparations are rejected inside the transaction; confirmations
match the immediately prior revision and operation-specific phrases; competing
preparations share a unique logical action. Frozen method/path/version/payload and
authorization are stored before dispatch. Settlement, receipt event and canonical
receipt message commit together, and history exposes owner-scoped structured receipts.

Context fitting compacts large list results across exchanges before eviction and retains
planner facts and structured mutation evidence. Unchanged cycles of up to four batches
receive one response-only recovery attempt inside existing caps. Successful final-step
writes require persisted action evidence for deterministic reporting. Generated schema
display titles are omitted to preserve useful input capacity.
