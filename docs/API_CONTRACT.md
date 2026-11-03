# Implementation integration contract

All backend paths retain `/api` through the Next.js same-origin proxy. JSON errors use `detail` (string). Cookie auth; mutation requests require matching Origin when present, and frontend sends same-origin requests. Do not read or expose `.env` contents in tools/logs. No real mutations in tests.

## Browser API

- `GET /api/health` → `{status:"ok"}`.
- `POST /api/auth/register` and `/api/auth/login`: `{email,password}` → `{id,email,time_zone,preferred_name}` and session cookie. Registration signs in. `preferred_name` is nullable and saved through chat when the user supplies their own form of address.
- `GET /api/auth/me` → user object or 401. `POST /api/auth/logout` → 204.
- `GET /api/connection` → `{connected:boolean, label:string|null}` (masked only).
- `PUT /api/connection`: `{api_key}` → masked connection status. `DELETE /api/connection` disconnects integration, not app account.
- `GET /api/chat/history` → `{messages:[{id,role,content,created_at,status}], next_cursor:string|null}`; optional `before` cursor, chronological order within page.
- `POST /api/chat/messages`: `{content,client_request_id}` → `{run_id,message_id,revision}`. UUID generated in browser; duplicate submission returns same result.
- `GET /api/runs/{id}` → `{id,status}`.
- `POST /api/runs/{id}/stop` → `{status,dispatch_committed:boolean}`.
- `GET /api/runs/{id}/events?after=N`: SSE frames with `id: N`, `event: TYPE`, `data: JSON`. Honor Last-Event-ID too. Events are durable and strictly increasing per run.
- Event types: `message.delta` `{message_id,text}`, `tool.status` `{text}`, `action.result` `{action_id,kind,summary,details}`, `run.waiting` `{message}`, `run.completed` `{}`, `run.failed` `{message}`, `run.interrupted` `{}`. Final three are terminal for the subscription; waiting is terminal for this run too.

Frontend must render safe Markdown, action receipts, readable errors and reconnection states; reload canonical history and never resubmit a message to reconnect. Credentials are never persisted in browser storage. Confirmation can use plain text yes/no through the same message endpoint.

Chat content rejected by local profanity/credential/instruction-override checks returns HTTP 422 with a safe `detail`, without creating a message/run or changing the conversation revision. A corrected submission can reuse that UUID. Model text is buffered and checked as a complete response before `message.delta` is persisted/published; clients must support a full reply in a single delta. An unchecked partial response is discarded on provider failure or interruption. A blocked response emits a fixed safe notice and `run.failed`. Earlier successful action receipts remain valid. History and replay filter legacy content at read time, including prohibited terms split across old delta events, without rewriting private action evidence.

## Provider Python interface (owned by provider agent)

`backend/app/providers/base.py` defines immutable dataclass `ProviderEvent`:
`kind: str` (`text_delta`, `tool_call`, `usage`, `done`), `text: str = ""`, `call_id: str = ""`, `name: str = ""`, `arguments: dict = {}` (default factory), `usage: dict = {}` (default factory).

`Provider` protocol: `stream(messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]` and `aclose() -> None` async. Messages/tool schemas use OpenAI Chat Completions-compatible dictionaries inside this adapter boundary. Completed tool requests only; arguments are assembled/validated before emitting `tool_call`. Exceptions are sanitized `ProviderError`.

`backend/app/providers/factory.py`: `create_provider(name: str, model: str | None = None, api_key: str | None = None) -> Provider`. Supported names `fake`, `openrouter`, `codex`. Fake is deterministic/offline, never silently selected on real-provider failure. OpenRouter key is a project secret, not user's Cal.com key. Codex is local-development only, explicit opt-in. Provider code owns no database or Cal.com execution.

Backend agent owns all `backend/` except `app/providers/` and `tests/test_providers*` (provider agent owns those). Backend agent owns pyproject and lockfile; include provider runtime dependencies `httpx` and optional Codex extra as verified/documented by provider agent. Provider agent must communicate dependency requirements in docs/provider-notes.md. No child commits: parent commits tested milestones. Frontend agent owns only `frontend/`. Parent owns root infrastructure/docs and integration.
