# HelpBook frontend

Next.js App Router, TypeScript, Tailwind CSS and free Headless UI components. Authentication uses backend session cookies. The app provides one ongoing conversation, Cal.com connection management, safe Markdown tables, action receipts, Stop and correction controls.

## Run

```sh
pnpm install --frozen-lockfile
pnpm dev --port 3002
```

The backend defaults to `http://localhost:8000`. Set `BACKEND_URL` to override it **before starting development or building production**. Next.js compiles rewrites into its production build; changing this variable only at `start` time does not change the destination. The Dockerfile accepts a `BACKEND_URL` build argument and defaults to `http://backend:8000`.

All browser calls go to same-origin `/api/...`; the Next.js external rewrite preserves that path and proxies response streams and cookies. Production TLS belongs at the deployment ingress. Do not enable response buffering for SSE at that ingress.

```sh
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm start
```

Tests use only mocked responses. They never invoke Cal.com or an LLM. Browser visual QA and Docker execution are outside these checks.

## Recovery and content safety

SSE events are parsed incrementally, deduplicated by durable ID and resumed using both `after` and `Last-Event-ID`. Transient disconnects reconnect with bounded backoff; a paused stream offers manual reconnect. Reconnection never submits a chat message. A tab remembers only its user-scoped active run ID in session storage; credentials stay in memory until submitted and are not put in browser storage. An ambiguous message POST can be explicitly retried with its original UUID. Closing or reloading the tab while that POST is unresolved loses that in-memory retry token; inspect canonical history before sending the request again.

Canonical history reloads after terminal events. Only booking creation appears as a structured receipt card, restored from history or streamed during a run. Event-type creation, rescheduling and cancellation results appear as readable chat messages. Confirmation questions appear once in chat with readable meeting details; entering the waiting state clears the progress text. The internal booking-creation message is omitted to avoid a duplicate JSON receipt. Receipt start and end times use the account's saved timezone and display the weekday, day-month-year date, 24-hour time and timezone. Rendering allows GFM tables, disables raw HTML and all images, limits links to HTTP(S)/mailto, and renders only allowlisted action-detail fields. No booking or availability data is fabricated in the UI.

Booking cards are interleaved with messages inside the conversation's scroll area. Their `created_at` and `message_id` identify the saved action-result message, so streaming, replay, reload and older-history pagination retain their original position. The scheduled appointment date does not determine their chat order.
