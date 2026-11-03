import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import auth, chat, connection
from app.core.config import Settings
from app.core.security import KeyVault
from app.db.database import initialize


def create_app(settings=None, provider=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        # Fail closed: no automatically generated or default secret.
        app.state.vault = KeyVault(settings.master_secret, settings.master_secret_previous)
        await asyncio.to_thread(initialize, settings.database_path)
        runner = None
        task = None
        runtime_provider = provider
        if settings.runner_enabled:
            from app.agent.runner import Runner

            if runtime_provider is None:
                from app.providers.factory import create_provider

                if settings.llm_provider == "codex":
                    raise RuntimeError(
                        "Codex runtime isolation is not verified; adapter is disabled. "
                        "See docs/provider-notes.md. Select LLM_PROVIDER=fake explicitly for "
                        "offline demo, or configure openrouter with LLM_MODEL and a project "
                        "OPENROUTER_API_KEY. No paid fallback is automatic."
                    )
                try:
                    runtime_provider = create_provider(
                        settings.llm_provider,
                        model=settings.llm_model,
                        api_key=settings.openrouter_api_key
                        if settings.llm_provider == "openrouter"
                        else None,
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
    buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def guards(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_origin.rstrip("/"):
                return JSONResponse({"detail": "Origin is not allowed."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request denied."}, status_code=403)
            if request.url.path.startswith(("/api/auth/", "/api/chat/")):
                address = request.client.host if request.client else "unknown"
                key = (address, request.url.path)
                now = monotonic()
                bucket = buckets[key]
                while bucket and bucket[0] < now - 60:
                    bucket.popleft()
                if len(bucket) >= 30:
                    return JSONResponse(
                        {"detail": "Too many requests; retry in a minute."}, status_code=429
                    )
                bucket.append(now)
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
    return app


app = create_app()
