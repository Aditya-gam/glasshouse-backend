# Glasshouse backend — developer entrypoints. `make help` lists them.
.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help sync hooks dev up down logs test migrate openapi lint fmt typecheck check

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

sync: ## Install/refresh dependencies (uv)
	uv sync

hooks: ## Install the pre-commit git hooks
	uv run pre-commit install --install-hooks
	uv run pre-commit install --hook-type pre-push

dev: ## Bring up the local stack (Postgres + Redis + hot-reload API)
	$(COMPOSE) up --build

up: ## Start the backing services in the background (db + redis)
	$(COMPOSE) up -d db redis

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail the stack logs
	$(COMPOSE) logs -f

test: ## Run the test suite (spins a real Postgres via testcontainers)
	uv run pytest

migrate: ## Apply database migrations (available from M0.4)
	uv run alembic upgrade head

openapi: ## Export the OpenAPI schema to openapi.json (the published contract)
	uv run python -m scripts.export_openapi

lint: ## Lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type check (mypy --strict)
	uv run mypy .

check: lint typecheck ## All static checks (CI mirror)
