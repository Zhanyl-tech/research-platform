PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
# uv, when present, creates the venv with a supported interpreter and installs
# into it (uv-created venvs have no pip). Force the pip path with `make UV=`.
UV ?= $(shell command -v uv 2>/dev/null)

.PHONY: help install demo test lint typecheck fmt check ingest bench clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/rplat: pyproject.toml
ifdef UV
	@test -d $(VENV) || $(UV) venv --python ">=3.11" $(VENV)
	@$(UV) pip install --python $(BIN)/python -q -e ".[dev]"
else
	@test -d $(VENV) || { \
		$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
		|| { echo "rplat needs Python 3.11+; '$(PY)' is $$($(PY) --version 2>&1)." \
		          "Run e.g. 'make PY=python3.12 ...' or install uv."; exit 1; }; \
		$(PY) -m venv $(VENV); }
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
endif
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

check: lint typecheck test ## Lint, types and tests (CI adds a format check, coverage gate, demo)

ingest: install ## Build data/research.duckdb from the fixture
	@$(BIN)/rplat ingest --force

bench: install ## Append throughput and as-of read cost, with the machine printed first
	@$(BIN)/python bench/ingest.py
	@$(BIN)/python bench/asof.py

clean: ## Remove venv, caches and the built store
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .hypothesis data
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
