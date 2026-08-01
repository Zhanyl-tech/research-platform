PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help install demo test lint typecheck fmt check ingest clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/rplat: pyproject.toml
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
	@touch $(BIN)/rplat

install: $(BIN)/rplat ## Create the venv and install with dev extras

demo: install ## The point-in-time walkthrough — no network, no credentials
	@$(BIN)/rplat demo

test: install ## Run the test suite
	@$(BIN)/python -m pytest

lint: install ## ruff check
	@$(BIN)/ruff check .

fmt: install ## ruff format
	@$(BIN)/ruff format .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

check: lint typecheck test ## Everything CI runs

ingest: install ## Build data/research.duckdb from the fixture
	@$(BIN)/rplat ingest --force

clean: ## Remove venv, caches and the built store
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache data
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
