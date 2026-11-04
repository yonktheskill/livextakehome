# Implementation integration contract

All backend paths retain `/api` through the Next.js same-origin proxy. JSON errors use `detail` (string). Cookie auth; mutation requests require matching Origin when present, and frontend sends same-origin requests. Do not read or expose `.env` contents in tools/logs. No real mutations in tests.

## Browser API

- `GET /api/health` → `{status:"ok"}`.
- `GET /api/auth/config` → `{registration_enabled}` (public runtime flag, no credentials).
- With `REGISTRATION_ENABLED=false`, `POST /api/auth/register` returns 403 without creating a user or session; existing-account login is unaffected. `TEST_ACCOUNT_ENABLED=true` provisions the shared test account once at startup, preserving existing accounts.
- `POST /api/auth/register` and `/api/auth/login`: `{email,password}` → `{id,email,time_zone,preferred_name}` and session cookie. Registration signs in. `preferred_name` is nullable and saved through chat when the user supplies their own form of address.
- `GET /api/auth/me` → user object or 401. `POST /api/auth/logout` → 204.
- `PATCH /api/auth/me`: `{time_zone}` (IANA timezone) → updated user object. The choice persists across sessions and takes precedence over the Cal.com timezone on reconnect. Changing it interrupts active runs and supersedes undispatched plans based on the old timezone; already dispatched actions may finish.
- `GET /api/connection` → `{connected:boolean, label:string|null}` (masked only).
- `PUT /api/connection`: `{api_key}` → masked connection status. `DELETE /api/connection` disconnects integration, not app account.
- `GET /api/chat/history` → `{messages:[{id,role,content,created_at,status}], receipts:[{action_id,kind,summary,details}], next_cursor:string|null}`; optional `before` cursor, chronological order within page. Receipts contain only booking creation; other completed actions appear as readable assistant messages.
- `POST /api/chat/messages`: `{content,client_request_id}` (content: 1–6,000 Unicode characters) → `{run_id,message_id,revision}`. UUID generated in browser; duplicate submission returns same result.
- `GET /api/runs/{id}` → `{id,status}`.
- `POST /api/runs/{id}/stop` → `{status,dispatch_committed:boolean}`.
- `GET /api/runs/{id}/events?after=N`: SSE frames with `id: N`, `event: TYPE`, `data: JSON`. Honor Last-Event-ID too. Events are durable and strictly increasing per run.
- Event types: `message.delta` `{message_id,text}`, `tool.status` `{text}`, `action.result` `{action_id,kind,summary,details}`, `run.waiting` `{message}`, `run.completed` `{}`, `run.failed` `{message}`, `run.interrupted` `{}`. Final three are terminal for the subscription; waiting is terminal for this run too.

Frontend must render safe Markdown, action receipts, readable errors and reconnection states; reload canonical history and never resubmit a message to reconnect. Credentials are never persisted in browser storage. Confirmation can use plain text yes/no through the same message endpoint.

Only `action.result` events with `kind: "create_booking"` render a result card. Event-type creation, rescheduling and cancellation also publish their readable result through `message.delta`. Confirmation questions arrive through `message.delta` and remain in chat history; `run.waiting` ends the run and clears progress text without displaying the same question again.

Receipts in history and new `action.result` events also include `message_id` and `created_at` (ISO UTC) from the canonical action-result message. Clients interleave cards with chat messages using this creation time and the anchor ID for ties, inside the conversation scroll area. `message.delta` events include the canonical message's `created_at` as well, so live order does not depend on the browser clock. Existing receipt anchors supply these fields on history reads without a database migration.

Chat content rejected by local credential checks returns HTTP 422 with a safe `detail`, without creating a message/run or changing the conversation revision. A corrected submission can reuse that UUID. Model text is buffered and checked as a complete response before `message.delta` is persisted/published; clients must support a full reply in a single delta. An unchecked partial response is discarded on provider failure or interruption. A blocked response emits a fixed safe notice and `run.failed`. Earlier successful action receipts remain valid. History and replay filter legacy content at read time, including credentials split across old delta events, without rewriting private action evidence.

## Provider Python interface (owned by provider agent)

`backend/app/providers/base.py` defines immutable dataclass `ProviderEvent`:
`kind: str` (`text_delta`, `tool_call`, `usage`, `done`), `text: str = ""`, `call_id: str = ""`, `name: str = ""`, `arguments: dict = {}` (default factory), `usage: dict = {}` (default factory).

`Provider` protocol: `stream(messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]` and `aclose() -> None` async. Messages/tool schemas use OpenAI Chat Completions-compatible dictionaries inside this adapter boundary. Completed tool requests only; arguments are assembled/validated before emitting `tool_call`. Exceptions are sanitized `ProviderError`.

`backend/app/providers/factory.py`: `create_provider(name: str, model: str | None = None, api_key: str | None = None) -> Provider`. Supported names `fake`, `openrouter`, `codex`. Fake is deterministic/offline, never silently selected on real-provider failure. OpenRouter key is a project secret, not user's Cal.com key. Codex is local-development only, explicit opt-in. Provider code owns no database or Cal.com execution.

Backend agent owns all `backend/` except `app/providers/` and `tests/test_providers*` (provider agent owns those). Backend agent owns pyproject and lockfile; include provider runtime dependencies `httpx` and optional Codex extra as verified/documented by provider agent. Provider agent must communicate dependency requirements in docs/provider-notes.md. No child commits: parent commits tested milestones. Frontend agent owns only `frontend/`. Parent owns root infrastructure/docs and integration.

## Private image attachments

`POST /api/chat/attachments` accepts a raw JPEG/PNG/WebP body with its matching
`Content-Type` (not multipart). Authentication, origin checks and chat rate limits apply.
The stream is bounded to 2 MiB even without, or with misleading, Content-Length.
Returns `{id,mime_type,width,height,size_bytes}` for normalized metadata-free pixels.
Only static images with edge <=4096 and <=8,000,000 pixels are accepted; errors use
413 for bytes, 415 for unsupported/mismatched formats or animation, and 422 for decode
or dimensions. No original duplicate is retained.

`GET /api/chat/attachments/{id}` returns owner-scoped normalized bytes, `private,
no-store` caching and `nosniff`; missing, expired unsent and foreign IDs return 404.
Unsent uploads expire after 24h. Sent uploads follow message deletion via cascade.

`POST /api/chat/messages` now accepts `{content:"",client_request_id,attachment_ids:[]}`.
Content remains capped at 6000 characters and must be nonblank unless attachments exist.
Supply at most four distinct ordered upload UUIDs owned by the user and not already
linked to another message. Acceptance claims uploads atomically; invalid IDs do not
increment revision or interrupt a run. Existing client UUID replay returns its original
message/run before claiming attachments, including after they have been linked. History
messages include `attachments: [{id,mime_type,width,height,size_bytes}]`, never bytes.
Image bytes/base64 are absent from SSE, diagnostic traces and ordinary scheduling context.
A successful, versioned extraction cache is separate from immutable Message.content.
All mutation request bodies are bounded (32 KiB except the 2 MiB upload route).

## Durable booking requests and autonomy

Original messages enter an owner-scoped semantic inbox. The ordinary model loop uses
`request_context` and `resolve_requests` to preserve independent requests and distinguish
new work from partial amendments or ambiguity. Booking writes carry `request_id` and
`request_revision`; completed requests retain their actual booking UID for rescheduling.
Pending interpretations and unknown outcomes block stale/duplicate mutations. Clear
instructions authorize actions; `authorize_action` records exact original-user evidence
for semantic cancellation/type creation when conservative local recognition is insufficient.
A final assistant reply does not mark unresolved work completed; the run remains waiting.
No browser API shape change is required. See `docs/agent-policy.md`.
