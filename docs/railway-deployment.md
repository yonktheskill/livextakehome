# Railway deployment through GitHub Actions

Production URL: https://livex.hongxu.dev

Railway fallback URL: https://frontend-production-739e1.up.railway.app

Both services are hosted in US West. Cloudflare DNS routes `livex` to
`ur6qi1kt.up.railway.app` with DNS-only CNAME routing and Railway-managed TLS.

Project: `3a12efd2-c496-4e46-85d4-162a8dcc7ac1`, environment:
`0662e4db-04f1-4293-bfae-3bc8a8e00af9` (`production`). Railway and GitHub
account emails can differ: the environment-scoped Railway project token authorizes
the deployment independently of the GitHub identity.

`.github/workflows/deploy.yml` uploads the backend and frontend separately after
the Checks workflow succeeds on main. It checks out the exact checked commit.
Manual workflow dispatch is also available. The `production` GitHub environment
uses the repository secret `RAILWAY_TOKEN`, an environment-scoped project token.
Railway requires account verification before creating that token.

The Railway services use their respective Dockerfiles. Service settings are
configured directly in Railway; deprecated Railway config files are not used.
The frontend builds with `BACKEND_URL=http://backend.railway.internal:8000` and
listens on port 3000. The backend uses `python -m app.serve` and listens on a dual-stack socket on port
8000 for both IPv4 health checks and IPv6 private networking,
runs one process/replica, and checks `/api/health`. Only the frontend has a public
domain. The frontend health check is `/login`.

The backend mounts its private persistent volume at `/app/data`. The database
is `/app/data/app.sqlite3`; the dedicated Codex login is `/app/data/codex/auth.json`.
The entrypoint prepares mounted directory ownership, restricts the login file to
mode 0600, and drops root before starting the server. No credentials are included
in source, build arguments, images, or GitHub Actions artifacts. Back up both the
database and Railway's `MASTER_SECRET` separately.

Backend settings include `PUBLIC_ORIGIN` equal to the HTTPS production URL,
`COOKIE_SECURE=true`, `REGISTRATION_ENABLED=false`, `TEST_ACCOUNT_ENABLED=true`,
`LLM_PROVIDER=codex`, `LLM_MODEL=gpt-6.1-sol`, `LIVEX_CODEX_ENABLED=true`, and
`LIVEX_CODEX_AUTH_HOME=/app/data/codex`. The shared demo accounts are
`test@testingmail.com` and `test2@testingmail.com`.
Each starts without a Cal.com connection;
connect a dedicated test Cal.com account through the app's connection settings.

The manual `Provision private Codex login` workflow stages the temporary GitHub
secret `CODEX_LOGIN_BOOTSTRAP` as a backend runtime variable. The next deployment
consumes it into `/app/data/codex/auth.json` with mode 0600, without overwriting
an existing login. After successful provisioning, remove the bootstrap variable
from Railway and the temporary GitHub secret, then redeploy. No bootstrap value
is used by the Dockerfile.

Alternatively, provision the dedicated account login privately after deployment,
using Railway's file upload/SSH facilities. Never print its contents. For example:

```sh
railway volume files --volume 3a46da21-1810-41a5-bc17-896718225182 upload /absolute/private/login/auth.json /codex/auth.json
```

Restart the backend after initial provisioning so the entrypoint applies file
ownership. The runtime reads this login through an isolated temporary home with
application execution tools disabled. Existing adapter limits still apply:
credentials refreshed by atomic replacement in that temporary home are discarded,
so account expiration may require provisioning a fresh login. See
[Codex adapter](codex-development.md) for its execution boundaries and limits.

OpenRouter remains available: set `LLM_PROVIDER=openrouter`, `LLM_MODEL` to the
desired OpenRouter model, and backend-only `OPENROUTER_API_KEY`. This requires no
image change. Providers never fall back automatically. Voice is configured
separately through backend-only `VOICE_API_KEY` and `VOICE_ENABLED`.
