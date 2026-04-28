.PHONY: help install lint format test cov precommit clean serve docker-build docker-run \
        compose-up compose-down compose-init compose-logs compose-pull

.DEFAULT_GOAL := help

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install package with dev extras and pre-commit hooks
	python -m pip install --upgrade pip
	pip install -e ".[dev]"
	pre-commit install --install-hooks

lint: ## Run ruff lint and format check
	ruff check .
	ruff format --check .

format: ## Auto-format with ruff
	ruff format .
	ruff check --fix .

test: ## Run the test suite
	pytest -q

cov: ## Run tests with coverage gate (>=95%)
	pytest --cov=coracle --cov-report=term-missing --cov-fail-under=95

precommit: ## Run pre-commit on all files
	pre-commit run --all-files

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +

serve: ## Run the coracle CLI (placeholder)
	python -m coracle

IMAGE ?= coracle:slim

docker-build: ## Build the slim production image (coracle:slim)
	DOCKER_BUILDKIT=1 docker build -t $(IMAGE) --target runtime .

docker-run: ## Run the slim image with sensible local defaults
	docker run --rm -p 8000:8000 \
		-v $(PWD)/config:/etc/coracle:ro \
		-v coracle-data:/var/lib/coracle \
		$(IMAGE)

# --- docker compose -----------------------------------------------------

compose-up: ## Bring up the compose stack (ollama + coracle)
	docker compose up -d

compose-down: ## Stop the compose stack (preserves named volumes)
	docker compose down

compose-init: ## One-shot: pull the default model set into the ollama volume
	docker compose --profile init up --exit-code-from ollama-init ollama-init

compose-logs: ## Tail logs from the running compose stack
	docker compose logs -f --tail=200

compose-pull: ## Pull the latest base images referenced by compose.yaml
	docker compose pull
