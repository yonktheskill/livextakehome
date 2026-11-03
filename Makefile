SHELL := /bin/sh
-include .env.local
export

.PHONY: help setup init-dev dev dev-backend dev-frontend up down logs migrate cleanup-traces format lint typecheck test test-backend test-frontend build check
help:
	@echo "setup / init-dev   Install dependencies / create private local configuration"
	@echo "dev               Start frontend and backend (Ctrl-C stops both)"
	@echo "check             Lint, format checks, typecheck and offline tests"
	@echo "up / down / logs  Docker Compose lifecycle"
	@echo "migrate / cleanup-traces / build"
setup:
	uv sync --project backend --python 3.13
	pnpm --dir frontend install --frozen-lockfile
init-dev:
	python3 scripts/init_dev.py
dev:
	@$(MAKE) dev-backend & backend_pid=$$!; $(MAKE) dev-frontend & frontend_pid=$$!; \
	trap 'kill $$backend_pid $$frontend_pid 2>/dev/null || true' INT TERM EXIT; wait
dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
dev-frontend:
	pnpm --dir frontend dev
up:
	docker compose --env-file .env.local up --build -d
down:
	docker compose --env-file .env.local down
logs:
	docker compose --env-file .env.local logs -f
migrate:
	cd backend && uv run python -m app.maintenance migrate
cleanup-traces:
	cd backend && uv run python -m app.maintenance cleanup-traces
format:
	cd backend && uv run ruff format app tests
lint:
	cd backend && uv run ruff check app tests
	cd backend && uv run ruff format --check app tests
	pnpm --dir frontend lint
typecheck:
	cd backend && uv run mypy app
	pnpm --dir frontend typecheck
test: test-backend test-frontend
test-backend:
	cd backend && uv run pytest -q
test-frontend:
	pnpm --dir frontend test
build:
	pnpm --dir frontend build
check: lint typecheck test
