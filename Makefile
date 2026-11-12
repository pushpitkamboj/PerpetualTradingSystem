COMPOSE ?= docker compose
PYTEST ?= uv run pytest
SERVICE ?=

.DEFAULT_GOAL := help

.PHONY: help build up up-d down restart logs ps config redis-cli shell-api test test-e2e clean

help: ## Show available commands.
	@awk 'BEGIN {FS = ":.*## "; printf "\nPython Perps commands:\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nExamples:\n"
	@printf "  make up\n"
	@printf "  make logs SERVICE=fastapi\n\n"

build: ## Build all Docker images.
	$(COMPOSE) build

up: ## Build and run the full stack in the foreground.
	$(COMPOSE) up --build

up-d: ## Build and run the full stack in the background.
	$(COMPOSE) up -d --build

down: ## Stop and remove containers.
	$(COMPOSE) down

restart: down up-d ## Restart the stack in the background.

logs: ## Follow logs. Optionally pass SERVICE=fastapi.
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

ps: ## Show running service status.
	$(COMPOSE) ps

config: ## Validate and print resolved Docker Compose config.
	$(COMPOSE) config

redis-cli: ## Open redis-cli inside the Redis container.
	$(COMPOSE) exec redis redis-cli

shell-api: ## Open a shell inside the FastAPI container.
	$(COMPOSE) exec fastapi sh

test: ## Run the local test suite.
	$(PYTEST) -m "not e2e"

test-e2e: ## Run end-to-end tests against Docker Compose.
	$(PYTEST) -m e2e app/packages/test_e2e.py

clean: ## Stop containers and remove volumes.
	$(COMPOSE) down -v --remove-orphans
