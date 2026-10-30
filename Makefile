# Developer shortcuts. On Windows use Git Bash / WSL, or run the underlying commands directly.
COMPOSE      ?= docker compose
TF_DIR       := infra/terraform
IMAGE        ?= crypto-quant:local

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test cov docker-build up up-all down logs ps tf-init tf-plan tf-apply tf-fmt

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Editable install with dev tools + git hooks
	pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2.0"
	pip install -e ".[dev]"
	pre-commit install

lint: ## Ruff lint + format check
	ruff check .
	ruff format --check .

format: ## Auto-fix lint and format
	ruff check . --fix
	ruff format .

typecheck: ## Mypy
	mypy src

test: ## Unit/integration tests
	pytest

cov: ## Tests with coverage report
	pytest --cov --cov-report=term-missing

docker-build: ## Build image locally
	docker build -t $(IMAGE) --build-arg VCS_REF=$$(git rev-parse --short HEAD) .

up: ## Start bot + GEX collector
	$(COMPOSE) up -d --build

up-all: ## Start bot + collector + monitoring stack
	@mkdir -p secrets && touch secrets/discord_webhook_url
	$(COMPOSE) --profile monitoring up -d --build

down: ## Stop everything
	$(COMPOSE) --profile monitoring down

logs: ## Follow bot logs
	$(COMPOSE) logs -f bot gex-collector

ps: ## Container status incl. health
	$(COMPOSE) --profile monitoring ps

tf-init: ## terraform init
	terraform -chdir=$(TF_DIR) init

tf-fmt: ## terraform fmt
	terraform -chdir=$(TF_DIR) fmt -recursive

tf-plan: ## terraform plan (needs TF_VAR_hcloud_token)
	terraform -chdir=$(TF_DIR) plan -out=tfplan

tf-apply: ## terraform apply saved plan
	terraform -chdir=$(TF_DIR) apply tfplan
