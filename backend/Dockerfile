FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra codex
COPY app ./app
COPY migrations ./migrations
RUN useradd --create-home app && mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["python", "-m", "app.container_entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
