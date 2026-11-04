# Recovering ambiguous external actions

An `unknown` action blocks further writes for its conversation. Restart recovery
invalidates abandoned preparations and keeps committed prompts and receipts.
No recovery command resends a request or resets it to prepared.

Run maintenance against the same `DATABASE_PATH` as the service, from `backend`,
using the application's installed environment. Restrict database and CLI access
to trusted operators. Inspection includes private scheduling details.

1. Stop the service and preserve a database backup (including SQLite WAL using a
   SQLite-aware backup). Restart recovery must have marked any interrupted
   dispatch `unknown`; resolution rejects queued or running actions.
2. Run `python -m app.maintenance list-unknown` to discover blocked actions,
   then `python -m app.maintenance inspect-action ACTION_ID`. Save the returned
   digest and inspect the frozen HTTP path, payload, target, fingerprint,
   authorization evidence and prior result.
3. Independently inspect Cal.com, its audit trail, and resource state. A missing
   search result alone does not establish failure. If the evidence is ambiguous,
   leave the action unknown. Do not blindly resend.
4. Save the verified resource response as a JSON object, or a JSON object
   describing conclusive failure evidence. Resolve explicitly:

   ```sh
   python -m app.maintenance resolve-action ACTION_ID --digest DIGEST \
     --outcome succeeded --operator operator@example.com \
     --note 'Verified resource and audit trail externally; reference case 123' \
     --observation-file /secure/observation.json
   ```

Use `--outcome failed` only for verified non-success. Success requires a resource
identity and must match the frozen target where present. The command requires
unchanged evidence and an inactive unknown action, records the previous frozen
evidence, operator, note and observation in a durable `action.reconciled` event,
and creates the usual canonical receipt on success, atomically. Resolution is
one-way; a second attempt fails. The original run status and failure events remain
historical evidence. A new user request can proceed after resolution; it is not
submitted automatically. Local operator names are attestations, not authenticated
identities; database access is the authorization boundary.

# Rate limiting behind ingress

Chat quotas are per authenticated user, shared across their sessions and chat
routes. Unknown paths cannot allocate distinct route buckets. Buckets expire
after 60 idle seconds and have a fixed capacity; new identities are denied while
capacity is full. Limits are per application process.

Anonymous login and registration share a quota per direct peer address by default.
Forwarded headers are ignored by the application. The ASGI server must preserve
its actual transport peer: launch Uvicorn with `--no-proxy-headers`, or set
`FORWARDED_ALLOW_IPS=` (included in `.env.example`). Remove server-level proxy
header trust before enabling application-managed ingress validation; otherwise
the server may already have replaced the peer address. Existing `.env.local`
files must add this setting explicitly. For deployments behind an ingress, explicitly set
`TRUSTED_INGRESS_CIDRS` to a JSON list of only trusted proxy peer networks. Those
peers must overwrite `X-Real-IP` with one verified client address and prevent
clients from reaching the backend directly. Do not configure broad client
networks or trust incoming forwarded chains. Without this ingress configuration,
anonymous clients behind one proxy share its quota. Authenticated chat clients
do not share that proxy quota.
