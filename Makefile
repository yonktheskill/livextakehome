SHELL := /bin/sh
-include .env.local
export
export NEXT_TELEMETRY_DISABLED := 1
FRONTEND_PORT ?= 3002
PUBLIC_ORIGIN ?= http://localhost:$(FRONTEND_PORT)
BACKEND_EXTRAS = $(if $(filter codex,$(LLM_PROVIDER)),--extra codex,)

.PHONY: help setup init-dev dev dev-backend dev-frontend up down logs migrate cleanup-traces format lint typecheck test test-backend test-frontend test-tooling build check smoke-containers setup-codex codex-login test-codex
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
	@python3 scripts/dev_supervisor.py $(MAKE)
dev-backend:
	cd backend && uv run $(BACKEND_EXTRAS) uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
dev-frontend:
	env -u MASTER_SECRET -u MASTER_SECRET_PREVIOUS -u OPENROUTER_API_KEY -u LIVEX_CODEX_AUTH_HOME pnpm --dir frontend dev --port $(FRONTEND_PORT)
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
	pnpm --dir frontend format
lint:
	cd backend && uv run ruff check app tests
	cd backend && uv run ruff format --check app tests
	pnpm --dir frontend lint
	pnpm --dir frontend format:check
typecheck:
	cd backend && uv run mypy app
	pnpm --dir frontend typecheck
test: test-backend test-frontend test-tooling
test-backend:
	cd backend && uv run pytest -q
test-frontend:
	pnpm --dir frontend test
test-tooling:
	python3 -m unittest discover -s scripts/tests -v
build:
	pnpm --dir frontend build
check: lint typecheck test

smoke-containers:
	python3 scripts/smoke_containers.py

setup-codex:
	uv sync --project backend --extra codex --python 3.13
codex-login:
	cd backend && PYTHONPATH=. uv run --extra codex python ../scripts/codex_login.py
test-codex:
	cd backend && uv run --extra codex pytest -q tests/test_providers*.py
