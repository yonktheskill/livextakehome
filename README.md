# Daylight — conversational Cal.com booking assistant

Python/FastAPI backend, Peewee/SQLite storage, and Next.js/TypeScript frontend with Tailwind CSS and free Headless UI. Design and product decisions: [PLAN.md](PLAN.md). API boundary: [docs/API_CONTRACT.md](docs/API_CONTRACT.md).

## Local development

Requires Python 3.13 (uv can install it), uv, Node 22+, and pnpm 10.32.1.

```sh
make setup
make init-dev
```

`init-dev` creates a private `.env.local` with a newly generated encryption key. It never reads or changes the existing `.env`, and refuses to overwrite an existing configuration. Keep this master key backed up separately from the database; losing it prevents decrypting saved Cal.com keys.

Choose the provider in `.env.local` before starting:

- `LLM_PROVIDER=codex` is the intended local account-based development path, but is **currently disabled** pending verified runtime isolation. See [provider notes](docs/provider-notes.md). There is no automatic paid fallback.
- `LLM_PROVIDER=fake` explicitly selects an offline UI/development demo. It returns labelled canned text, does not understand scheduling, and makes no bookings.
- `LLM_PROVIDER=openrouter` requires a project `OPENROUTER_API_KEY` and explicit `LLM_MODEL`. Usage is billed by that provider; keys stay backend-only. Scheduling quality and live model availability have not been evaluated yet.

```sh
make dev
```

Open `http://localhost:3000`, register, and enter your Cal.com API key through connection settings. The Cal.com key in the existing `.env` is not automatically imported into any account. Never enter a key into chat. Each user has one encrypted Cal.com connection and one ongoing conversation.

The frontend proxies same-origin `/api` requests to the backend on port 8000. To run separately, use `make dev-backend` and `make dev-frontend` in separate terminals. Next.js reads `BACKEND_URL` when starting development or building production.

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

```sh
make up
make logs
make down
```

Compose builds two application containers. Only the frontend is published, bound to localhost:3000; the backend uses a persistent named SQLite volume. `down` preserves that volume. Never remove the volume when merely restarting. Back up SQLite using its backup mechanism, including consistent WAL handling.

For public deployment, place HTTPS ingress in front, set `PUBLIC_ORIGIN` to its exact origin and `COOKIE_SECURE=true`, and configure the ingress to flush SSE without buffering. Store the master encryption key and project model key as deployment secrets. A personal Codex login must never serve public users. Production frontend proxy rewrites use a build-time `BACKEND_URL`; Compose supplies the internal backend address.

## Scheduling boundary

The assistant manages **Bookings** through **Event Types** and **Slots**. Cal Events (tickets/RSVPs) and arbitrary Google/Outlook calendar entries are outside scope. A reusable event type is not an appointment. Cancellation retains booking history. Clear booking/reschedule instructions execute once resolved; cancellation and creating a missing type require the agreed confirmation flow.

Actions and response chunks are persisted before being published. A transactional dispatch claim orders incoming corrections against writes. SQLite cannot atomically commit an external Cal.com request; uncertain outcomes need reconciliation before another potentially duplicate write. See the plan for the exact boundary and acceptance requirements.

## Implementation status

Frontend, backend, and provider milestones are implemented. Final hardening and review are in progress. Both Docker images build. Real HTTP two-container tests passed cookies/auth, proxying, dedupe, SSE/history, CSRF, restart persistence, and logout using explicit fake inference. Live Cal.com writes, real model execution, and browser visual verification are not yet certified. Contacts, screenshot input, persistent preferences, and voice remain future work.
