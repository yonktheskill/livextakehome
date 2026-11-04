# Browser recovery and local development

Unconfirmed chat submissions retain only their content and original client request
UUID in user-scoped sessionStorage for the current browser tab. Auth cookies,
passwords, and Cal.com API keys are never included in this record. Treat chat text
as private browser data; closing the tab clears it, and successful sign-out removes
that user's recovery records. Browser storage must be available before sending.

Reload restores the unconfirmed message and offers **Retry message**. It never
posts automatically. Retry uses the same UUID, so the server can return the
original acceptance even when the first POST succeeded but its response was lost.
A saved run reconnects after history loads, including after **Reload conversation**
recovers from an initial history failure. No active-run discovery endpoint is needed
for this case: explicit retry recovers the run ID from idempotent acceptance.

Connection changes refresh the authenticated profile without the loading screen
that would unmount the conversation. This updates the account timezone while
preserving draft text and the stream.

`make dev` uses a Python POSIX supervisor (macOS/Linux). Each service gets a process
group. A service exit stops both groups and propagates its exit status; Ctrl-C
returns 130. Cleanup includes descendants even after their make process exits.
Run its offline lifecycle regressions with:

```sh
python3 -m unittest discover -s scripts/tests -v
```

## Encryption key rotation with Compose

Keep `.env.local` private. Set `MASTER_SECRET` to the new Fernet key and
`MASTER_SECRET_PREVIOUS` to a JSON array of old Fernet keys, for example
`["<old-key>"]`. Compose explicitly forwards both values, including command-line
overrides used by Make. Backend Settings parses the array and MultiFernet validates
every key at startup. Do not use a comma-separated string or remove old keys while
stored data still requires them. Recreate the backend after changing configuration.
New encryption uses the first/current key; existing ciphertext remains readable
with the previous keys. This configuration does not itself re-encrypt existing
records. Retire old keys only after migrating or replacing all dependent ciphertext
and accounting for backups that still need those keys.
