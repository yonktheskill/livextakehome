# HelpBook — conversational Cal.com booking assistant

Python/FastAPI backend, Peewee/SQLite storage, and Next.js/TypeScript frontend with Tailwind CSS and free Headless UI. Design and product decisions: [PLAN.md](PLAN.md). API boundary: [docs/API_CONTRACT.md](docs/API_CONTRACT.md).

## Local development

Requires Python 3.13 (uv can install it), uv, Node 22+, and pnpm 10.32.1.

```sh
make setup
make init-dev
```

`init-dev` creates a private `.env.local` with a newly generated encryption key. It never reads or changes the existing `.env`, and refuses to overwrite an existing configuration. Keep this master key backed up separately from the database; losing it prevents decrypting saved Cal.com keys.

All feature flags and runtime settings are configured together in `.env.local`.
[`.env.example`](.env.example) is the complete, grouped template used by `init-dev`;
[`Settings`](backend/app/core/config.py) defines backend types, validation and defaults.
This includes `MAX_CONCURRENT_RUNS`, `RUNNER_ENABLED`, `LIVEX_CODEX_ENABLED`,
`DEBUG_API`, provider credentials, context budgets and optional TLS trust settings.
Make explicitly exports this file, and Compose passes it to the backend container.
The backend itself reads only its supplied environment; explicit `Settings(...)`
values override that environment. Restart the backend to apply changes. For an
existing `.env.local`, copy any additional settings you need from the template.

For account-backed development, `init-dev` selects Codex and a dedicated login directory outside this repository. Complete sign-in privately:

```sh
make setup-codex
make codex-login
make dev
```

The login command starts account authentication only; it does not make bookings or run inference. Check `LLM_MODEL` in `.env.local` against models available to your account. Native local Codex development supports macOS/Linux and uses the pinned runtime. See [Codex setup, isolation evidence and limits](docs/codex-development.md).

Provider choices in `.env.local`:

- `LLM_PROVIDER=codex` uses your dedicated account login with explicit local opt-in. Model tools are disabled and returned application-tool proposals are validated by the backend. Both providers' replies are buffered for local content checks before being saved or streamed to the browser. There is no automatic paid fallback.
- `LLM_PROVIDER=fake` explicitly selects an offline UI/development demo. It returns labelled canned text, does not understand scheduling, and makes no bookings.
- `LLM_PROVIDER=openrouter` requires a project `OPENROUTER_API_KEY` and explicit `LLM_MODEL`. Usage is billed by that provider; keys stay backend-only. Scheduling quality and live model availability have not been evaluated yet.

```sh
make dev
```

The default frontend port is 3002 to avoid conflicts with other local apps. To choose another port, set both `FRONTEND_PORT` and the matching `PUBLIC_ORIGIN` in `.env.local`.

Open `http://localhost:3002`, sign in, and enter your Cal.com API key through connection settings. The Cal.com key in the existing `.env` is not automatically imported into any account. Never enter a key into chat. Each user has one encrypted Cal.com connection and one ongoing conversation.

The assistant receives the signed-in user's email for their own booking details. Email-derived names are unconfirmed hints. It asks how to address the user and can save or correct their preferred name through chat, even without a Cal.com connection. This profile persists across sessions; other people's contacts are not stored. Existing databases upgrade automatically at backend startup.

The frontend proxies same-origin `/api` requests to the backend on port 8000. To run separately, use `make dev-backend` and `make dev-frontend` in separate terminals. Next.js reads `BACKEND_URL` when starting development or building production.

The Cal.com client honors `SSL_CERT_FILE` and `SSL_CERT_DIR` in the backend environment for networks with a custom CA certificate chain. TLS certificate verification stays enabled, and ambient HTTP proxy settings remain ignored. A TLS/network failure during key validation does not indicate an invalid key or an uncertain booking outcome; validation only reads `/v2/me`.

## Account access

The template configures a closed public demo with `REGISTRATION_ENABLED=false` and
`TEST_ACCOUNT_ENABLED=true`. Existing `.env.local` files need these settings added
explicitly. Restart the backend after changing them; no frontend rebuild is needed.

`REGISTRATION_ENABLED=false` rejects account creation with HTTP 403, hides sign-up
links, and replaces `/register` with a closed-registration message. Existing users
can still sign in. Set it to `true` to reopen registration. The backend default is
`true` for compatibility when the variable is omitted.

`TEST_ACCOUNT_ENABLED=true` creates two accounts at startup, even with registration disabled:

- `test@testingmail.com` / `testtesttest`
- `test2@testingmail.com` / `testtesttest2`

Each account has its own conversation, and passwords are stored as Argon2 hashes.
Restarts preserve existing accounts, passwords, and data. This flag defaults to
`false` when omitted. Turning it off stops provisioning; it does not delete accounts.
Anyone using these shared credentials can access that account's conversation and connected Cal.com account; use demo data
and a dedicated test Cal.com account.

## Verification and maintenance

```sh
make check           # backend/frontend lint, types, offline tests
make build           # Next.js production build
make migrate         # initialize/upgrade the local schema
make cleanup-traces  # remove expired detailed debug records
make smoke-containers # verify built images over real HTTP, offline
```

Automated tests use temporary databases, fake providers, and mocked Cal.com HTTP. They do not send meetings, use the personal `.env`, or incur model charges. Provider-only SDK inspection may skip when its optional package is absent. Optional Git hooks: `uvx pre-commit install` (configuration is committed). See [frontend notes](frontend/README.md) for stream recovery behaviour and limitations.

## Live voice

Voice uses GPT-Live independently of `LLM_PROVIDER`; typed chat and delegated
scheduling requests continue through the configured agent (Codex SDK in local
development). Set `VOICE_ENABLED=true` and a backend-only `VOICE_API_KEY` in
`.env.local`, then restart the backend. Optional settings are `VOICE_BASE_URL`
(default `https://api.openai.com/v1`), `VOICE_MODEL` (`gpt-live-1`), `VOICE_VOICE`
(`marin`) and `VOICE_MAX_SECONDS` (`900`). Never put the key in a `NEXT_PUBLIC_`
variable. The gateway must support Live session creation and sideband WebSockets;
a Chat Completions-compatible endpoint alone is insufficient.

Use Start voice in the composer and allow microphone access on localhost or HTTPS.
Both speakers' captions update the existing chat timeline, and saved transcripts
become context for later typed messages. Mute disables microphone input; End voice
releases the microphone and speaker. Sending a typed message ends the active voice
session before submitting it through the normal chat route.

Say "end call", "hang up" or "goodbye" to end voice. The agent also has an `end_call`
tool. Once a booking succeeds and no other requests remain pending, voice finishes
the confirmation/farewell and closes automatically. Further speech keeps the call
open. The browser observes remote audio to allow playback to finish; conservative
timeouts cover blocked playback or lost connections. Explicit spoken hang-up also
recognizes acknowledgments such as "Okay, nice. End call"; it mutes input immediately
and releases browser media after the farewell, with a 10-second playback deadline.
A 15-second backend fallback starts closing the provider session if the browser
never acknowledges playback. A failed farewell send cannot prevent shutdown.
Backend clarifications and
their spoken captions share one reply; exact action-confirmation details appear
inside that reply when required.

Voice speech is played immediately without complete-response checks or a separate
moderation call. Backend scheduling validation, authorization, dispatch and receipts
still apply. Exact backend confirmation questions remain visible in chat. Transcript
timestamps are approximate and do not establish which words were heard before an
interruption. Audio recordings are not stored by this application; text transcripts
share normal conversation retention. Live voice requires a separately billed API
project key; the Codex account login does not supply it.

## Containers

For containers, select OpenRouter and configure its project model/key, or use `make up LLM_PROVIDER=fake` for an offline demo. The backend image includes the pinned Codex SDK, with login credentials supplied separately at runtime. See [Railway deployment](docs/railway-deployment.md).

```sh
make up
make logs
make down
```

Compose builds two application containers. Only the frontend is published, bound to localhost:3002; the backend uses a persistent named SQLite volume. `down` preserves that volume. Never remove the volume when merely restarting. Back up SQLite using its backup mechanism, including consistent WAL handling.

For public deployment, place HTTPS ingress in front, set `PUBLIC_ORIGIN` to its exact origin and `COOKIE_SECURE=true`, and configure the ingress to flush SSE without buffering. Store the master encryption key and project model key as deployment secrets. Account-backed Codex deployment requires explicit opt-in and a private persistent login directory; never bake a login into the image. Production frontend proxy rewrites use a build-time `BACKEND_URL`; Compose supplies the internal backend address.

## Scheduling boundary

The assistant manages **Bookings** through **Event Types** and **Slots**. Cal Events (tickets/RSVPs) and arbitrary Google/Outlook calendar entries are outside scope. A reusable event type is not an appointment. Host cancellation retains booking history and cancels the whole exact booking for all attendees, including seated bookings; any required clarification shows the live target and full scope. Recurring series cancellation and seated rescheduling remain unsupported. Clear booking/reschedule/cancellation instructions execute once their target and scope resolve. Implicit reusable-type creation needs permission unless already authorized. Independent requests remain pending; corrections amend only the identified request. See [agent policy and corrections](docs/agent-policy.md).

Actions and response chunks are persisted before being published. A transactional dispatch claim orders incoming corrections against writes. SQLite cannot atomically commit an external Cal.com request; uncertain outcomes need reconciliation before another potentially duplicate write. See the plan for the exact boundary and acceptance requirements.

## Concurrent runs

The backend runs a bounded pool of async workers. While one run waits for its model
or Cal.com response, other conversations can progress. Set `MAX_CONCURRENT_RUNS`
(default `4`, range `1–64`) in `.env.local` or the backend environment; `1` restores
serial execution. Excess runs remain in the persisted SQLite queue, without
creating an unbounded number of waiting tasks. Tune the limit to provider rate
limits and available resources; it bounds whole runs, including tool execution.

Run claims are atomic and skip conversations with a running job. A new message
still interrupts its conversation's previous run at existing checkpoints, and its
replacement waits until that run settles. Other conversations continue meanwhile.
Per-run diagnostics and budgets are isolated. On shutdown, all active workers are
cancelled and awaited before the shared provider closes. A dispatched write without
a recorded outcome becomes `needs_reconciliation`; queued work survives restart.

Use **one backend process/replica** with this SQLite queue. Startup recovery assumes
exclusive ownership of running jobs. Adding Uvicorn workers or replicas requires
worker ownership/leases and coordinated recovery before it is safe; the async pool
provides concurrency within the current process.

## Content guardrails

The backend rejects recognizable credentials before chat persistence and checks complete model responses and tool proposals before publication or dispatch. Names, harmless frustration and instruction-related words are accepted as scheduling data. Professional responses, harmful-action refusal and the untrusted-data boundary are system policy; keyword blacklists are not treated as a prompt-injection defense.

Credentials are filtered from external data, profile values, confirmations, receipts, history and replay. Original action evidence and truthful outcomes remain intact. Ownership, schemas, live availability, exact authorization evidence, request revisions and dispatch/recovery checks enforce the action boundary. These checks use no additional moderation service or model call.

## Implementation status

Frontend, backend, and provider milestones are implemented. Review findings have regression tests and fixes. Both Docker images build. Real HTTP two-container tests passed cookies/auth, proxying, dedupe, SSE/history, CSRF, restart persistence, and logout using explicit fake inference. Final checks passed 87 backend tests and 19 frontend tests, plus the 50-test provider suite with the optional SDK. A read-only Cal.com check using the supplied key could not establish a connection from this execution environment (no mutations were attempted). Live writes, signed-in model inference and browser visual verification remain unverified. The user's own preferred name, private screenshot messages and live voice integration are implemented; general contacts and other persistent preferences remain future work. Voice transport access has a live connection smoke check; browser microphone/playback behavior still requires live verification.

## Agent diagnostics

Chat submissions accept up to 6,000 Unicode characters in both the composer and
API. Oversized drafts remain editable with feedback; they are not silently clipped.
The 6,000-character limit leaves room for the system prompt, tool schemas and a
retrieval result within the default 250,000 estimated-token input budget;
escaping, unusual Unicode, action evidence or smaller configured budgets can still
cause a safe context-budget failure rather than dropping current intent.

Historical originals remain independently retrievable through `search_history`.
Search returns bounded segments with source/conversation IDs, role, original timestamp
and time zone. For a long source, call with its `source_id` and returned `next_offset`
(Unicode character offset into filtered text), continuing until `complete=true`.
`offset`, `end_offset` and `total_characters` support exact assembly; a suffix alone
is still `truncated`. Only contiguous full coverage counts as a covered source.
Exact lookups stay inside the run's conversation and historical time boundary;
`role` filters discovery, while an exact source ID can retrieve either role.
This pagination supplements the submission limit and also supports older longer rows.

Agent inference defaults to a 250,000 estimated-token input allowance, including tool
schemas (currently about 6,000 estimated tokens). This is half of GPT-6.1 Sol’s
500,000-token default Codex window, as documented in the
[official model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
`CONTEXT_WINDOW_TOKENS=500000` retains output and safety reserves. Configure both
window and input limits for a different model/provider. Provider transport accepts
up to 4 MB of text JSON, including escaping, so its byte cap supports this allowance.
The allowance is a ceiling; source deduplication and bounded history selection still apply. `CONTEXT_TOOL_RESULT_TOKENS=2500`
reserves room when selecting optional history and the protected original-request chain.
When fixed context leaves too little
room for that reserve, the remaining space is shared between original intent and tool
results; latest tool results are compacted individually if necessary. Original user
intent and complete call/result pairings remain protected. Request-tool responses
reference complete originals already protected in the prompt instead of echoing their
captions and image observations. Under pressure, other source payloads become explicit
`search_history` references; durable facts, revisions, IDs and ambiguity blockers remain.
Historical image excerpts carry source/attachment IDs rather than full extractions.
Large image originals use `source_format=image_source_json`: concatenate `content`
pages to recover JSON containing the original caption, attachments and observations.
Canonical text and cached extractions remain unchanged. Three consecutive
identical proposal batches with unchanged results stop with `repeated_calls`;
changing results or arguments count as progress.

The database `trace` table records operation starts, completed model/tool calls,
structured failures, context fitting decisions and repeated-call fingerprints.
Failures retain safe codes, stages, timings, budget consumption, application source
locations and available provider status/version/turn metadata. Context traces retain
source/call references, before/after estimates, compaction sizes and prompt/schema
hashes, without copying assembled prompts, credentials or model reasoning. Component
estimates identify system, action, profile, request state, original intent, current
request and tool-exchange costs; per-step assembly failures retain their budget audit. Tool
traces retain original byte/item counts even when their payload is truncated.
Trace persistence failures emit sanitized JSON to server logs; diagnostic failure
does not replace the original operation failure. Detailed traces expire after 30 days.

For local inspection, enable `DEBUG_API=true`. The authenticated, owner-scoped
`GET /api/runs/{run_id}/debug?after=0&limit=100` returns an ordered list containing
`id`, `created_at`, `kind` and `metadata`. Use the last returned `id` as `after` to
read the next page; maximum page size is 500. It is disabled by default.

### Private screenshot messages

Attach, paste or drop up to four static JPEG, PNG or WebP screenshots, optionally
with a caption. Original browser files are limited to 10 MiB each; processed images
must be at most 2 MiB each / 8 MiB combined, 4096 pixels per edge and 8 megapixels.
If a long screenshot cannot retain readability within these limits, crop or split it.
Images use authenticated private routes and dedicated previews; Markdown remote images
remain disabled. Pending retries store opaque attachment IDs and metadata in session
storage, never image bytes. Unsent uploads expire after 24 hours; sent images share
message lifetime. Run `cd backend && uv run python -m app.maintenance cleanup-attachments`
for explicit abandoned upload cleanup (also performed by upload/runner maintenance).

The configured provider performs one tool-free extraction before scheduling. Successful
versioned observations are persisted for retries, restart and followups; extraction adds
latency and image inference cost. Native image support is required by the configured
model; unsupported models fail without fallback. Fake mode stays offline and deterministic
and does not read actual pixels. Text captions/questions retain their conversational meaning.
Image observations cannot authorize cancellation, event type creation, profile changes or
confirmation. Schema validation and execution gates do not prove OCR truth or injection
resistance: confidently misread or forged meeting facts remain a material risk. No live
vision evaluation is implied by the offline tests. TODO: replace the small SQLite BLOB
storage boundary with private object storage before scaling image volume.
