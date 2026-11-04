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

Open `http://localhost:3002`, register, and enter your Cal.com API key through connection settings. The Cal.com key in the existing `.env` is not automatically imported into any account. Never enter a key into chat. Each user has one encrypted Cal.com connection and one ongoing conversation.

The assistant receives the signed-in user's email for their own booking details. Email-derived names are unconfirmed hints. It asks how to address the user and can save or correct their preferred name through chat, even without a Cal.com connection. This profile persists across sessions; other people's contacts are not stored. Existing databases upgrade automatically at backend startup.

The frontend proxies same-origin `/api` requests to the backend on port 8000. To run separately, use `make dev-backend` and `make dev-frontend` in separate terminals. Next.js reads `BACKEND_URL` when starting development or building production.

The Cal.com client honors `SSL_CERT_FILE` and `SSL_CERT_DIR` in the backend environment for networks with a custom CA certificate chain. TLS certificate verification stays enabled, and ambient HTTP proxy settings remain ignored. A TLS/network failure during key validation does not indicate an invalid key or an uncertain booking outcome; validation only reads `/v2/me`.

## Verification and maintenance

```sh
make check           # backend/frontend lint, types, offline tests
make build           # Next.js production build
make migrate         # initialize/upgrade the local schema
make cleanup-traces  # remove expired detailed debug records
make smoke-containers # verify built images over real HTTP, offline
```

Automated tests use temporary databases, fake providers, and mocked Cal.com HTTP. They do not send meetings, use the personal `.env`, or incur model charges. Provider-only SDK inspection may skip when its optional package is absent. Optional Git hooks: `uvx pre-commit install` (configuration is committed). See [frontend notes](frontend/README.md) for stream recovery behaviour and limitations.

## Containers

For containers, select OpenRouter and configure its project model/key, or use `make up LLM_PROVIDER=fake` for an offline demo. The production image excludes the optional native Codex SDK/login.

```sh
make up
make logs
make down
```

Compose builds two application containers. Only the frontend is published, bound to localhost:3002; the backend uses a persistent named SQLite volume. `down` preserves that volume. Never remove the volume when merely restarting. Back up SQLite using its backup mechanism, including consistent WAL handling.

For public deployment, place HTTPS ingress in front, set `PUBLIC_ORIGIN` to its exact origin and `COOKIE_SECURE=true`, and configure the ingress to flush SSE without buffering. Store the master encryption key and project model key as deployment secrets. A personal Codex login must never serve public users. Production frontend proxy rewrites use a build-time `BACKEND_URL`; Compose supplies the internal backend address.

## Scheduling boundary

The assistant manages **Bookings** through **Event Types** and **Slots**. Cal Events (tickets/RSVPs) and arbitrary Google/Outlook calendar entries are outside scope. A reusable event type is not an appointment. Cancellation retains booking history. Clear booking/reschedule instructions execute once resolved; cancellation and creating a missing type require the agreed confirmation flow.

Actions and response chunks are persisted before being published. A transactional dispatch claim orders incoming corrections against writes. SQLite cannot atomically commit an external Cal.com request; uncertain outcomes need reconciliation before another potentially duplicate write. See the plan for the exact boundary and acceptance requirements.

## Content guardrails

The backend rejects chat messages containing listed English profanity/slurs, common obfuscations, recognizable credentials, or narrowly matched instruction-override attempts. Rejected submissions do not enter conversation history or interrupt an existing run. The local policy list is `backend/app/core/profanity.txt`; matching uses word boundaries, so names such as Dickinson and Scunthorpe remain valid. Exact names that coincide with a listed term are rejected under the strict policy.

Each complete model response and all of its tool proposals are checked before that response is published or its tools run. Booking fields, names, event titles/descriptions, and cancellation reasons are checked before writes. Blocked responses produce a fixed safe notice. External tool data, profile values, confirmations, receipts, history and replay are filtered for display/context; original action evidence is retained privately, including truthful outcomes for writes completed before a later response was blocked. Profile values are provided as data outside the system message.

These checks use no extra service, model calls or dependencies. They are a finite English policy list and pattern checks, not semantic harmful-content moderation or a guarantee against prompt injection, every language, or every obfuscation. Harmful-content restrictions also appear in the system prompt; existing ownership, schema and confirmation controls remain the execution boundary. Sensitive appointments such as therapy and sexual health are allowed.

## Implementation status

Frontend, backend, and provider milestones are implemented. Review findings have regression tests and fixes. Both Docker images build. Real HTTP two-container tests passed cookies/auth, proxying, dedupe, SSE/history, CSRF, restart persistence, and logout using explicit fake inference. Final checks passed 87 backend tests and 19 frontend tests, plus the 50-test provider suite with the optional SDK. A read-only Cal.com check using the supplied key could not establish a connection from this execution environment (no mutations were attempted). Live writes, signed-in model inference and browser visual verification remain unverified. The user's own preferred name is persisted; general contacts, screenshot input, other persistent preferences, and voice remain future work.
