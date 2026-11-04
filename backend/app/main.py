import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api import attachments, auth, chat, connection
from app.core.config import Settings
from app.core.security import KeyVault, digest
from app.db.database import initialize, query


class RateBuckets:
    """Finite categories, idle expiry and bounded memory without quota resets."""

    def __init__(self, capacity=10000):
        self.capacity = capacity
        self.buckets = OrderedDict()

    def allow(self, key, now, limit=30):
        while self.buckets:
            oldest, bucket = next(iter(self.buckets.items()))
            if bucket[-1] > now - 60:
                break
            del self.buckets[oldest]
        bucket = self.buckets.get(key)
        if bucket is None:
            if len(self.buckets) >= self.capacity:
                return False
            bucket = deque()
            self.buckets[key] = bucket
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        self.buckets.move_to_end(key)
        return True


def auth_address(request, settings):
    peer = request.client.host if request.client else "unknown"
    try:
        trusted = any(
            ip_address(peer) in ip_network(cidr) for cidr in settings.trusted_ingress_cidrs
        )
        if trusted:
            # A single validated address, never an arbitrary forwarded chain.
            return str(ip_address(request.headers.get("x-real-ip", peer)))
    except ValueError:
        pass
    return peer


def create_app(settings=None, provider=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        # Fail closed: no automatically generated or default secret.
        app.state.vault = KeyVault(settings.master_secret, settings.master_secret_previous)
        await asyncio.to_thread(initialize, settings.database_path)
        if settings.test_account_enabled:
            from app.db.seed import seed_test_account

            await query(seed_test_account)
        runner = None
        task = None
        runtime_provider = provider
        if settings.runner_enabled:
            from app.agent.runner import Runner

            if runtime_provider is None:
                from app.providers.factory import create_provider

                if settings.llm_provider == "codex":
                    from importlib.metadata import PackageNotFoundError, version
                    from pathlib import Path

                    from app.providers.codex import SDK_VERSION

                    home = settings.livex_codex_auth_home
                    if (
                        not settings.livex_codex_enabled
                        or not settings.llm_model
                        or not home
                        or not Path(home).is_absolute()
                    ):
                        raise RuntimeError(
                            "Codex requires explicit development opt-in, LLM_MODEL and a "
                            "dedicated absolute LIVEX_CODEX_AUTH_HOME; "
                            "see docs/codex-development.md. No paid fallback is automatic."
                        )
                    try:
                        if version("openai-codex") != SDK_VERSION:
                            raise RuntimeError("Install the pinned Codex development extra.")
                    except PackageNotFoundError:
                        raise RuntimeError("Install with make setup-codex first.") from None
                try:
                    runtime_provider = create_provider(
                        settings.llm_provider,
                        model=settings.llm_model,
                        api_key=settings.openrouter_api_key
                        if settings.llm_provider == "openrouter"
                        else None,
                        codex_enabled=settings.livex_codex_enabled,
                        codex_auth_home=settings.livex_codex_auth_home,
                    )
                except Exception:
                    raise RuntimeError(
                        "Provider setup failed. Configure LLM_PROVIDER=codex with "
                        "the isolated local runtime, or openrouter with OPENROUTER_API_KEY. "
                        "Use fake explicitly only for offline demo/tests."
                    ) from None
            runner = Runner(settings, app.state.vault, runtime_provider)
            task = asyncio.create_task(runner.supervise(), name="agent-runner")
        try:
            yield
        finally:
            if runner:
                runner.stopping.set()
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if runtime_provider:
                await runtime_provider.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    buckets = RateBuckets()

    @app.middleware("http")
    async def guards(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            limit = 2 * 1024 * 1024 if request.url.path == "/api/chat/attachments" else 32_768
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    return JSONResponse({"detail": "Request body is too large."}, status_code=413)
                body.extend(chunk)
            request._body = bytes(body)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_origin.rstrip("/"):
                return JSONResponse({"detail": "Origin is not allowed."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request denied."}, status_code=403)
            category = (
                "auth"
                if request.url.path in {"/api/auth/register", "/api/auth/login"}
                else "chat"
                if request.url.path.startswith("/api/chat/")
                else None
            )
            if category:
                if category == "chat":
                    try:
                        user = await query(auth.current_sync, request.cookies.get(auth.COOKIE))
                    except HTTPException as exc:
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                    identity = str(user.id)
                else:
                    # Do not give every account behind Next.js the same small
                    # login quota. Account limits also survive IP/session changes.
                    try:
                        credentials = auth.Credentials.model_validate(await request.json())
                    except (ValueError, ValidationError):
                        return JSONResponse(
                            {"detail": "Invalid request; check required fields and formats."},
                            status_code=422,
                        )
                    identity = digest(str(credentials.email).lower())
                    # A wider source ceiling bounds floods across invented accounts.
                    if not buckets.allow(
                        ("auth-source", auth_address(request, settings)), monotonic(), limit=3000
                    ):
                        return JSONResponse(
                            {"detail": "Too many requests; retry in a minute."}, status_code=429
                        )
                if not buckets.allow((category, identity), monotonic()):
                    return JSONResponse(
                        {"detail": "Too many requests; retry in a minute."}, status_code=429
                    )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors contain submitted input, potentially including a password/API key.
        return JSONResponse(
            {"detail": "Invalid request; check required fields and formats."}, status_code=422
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse(
            {"detail": "Internal error; operation outcome may need review."}, status_code=500
        )

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(connection.router)
    app.include_router(chat.router)
    app.include_router(attachments.router)
    return app


app = create_app()
