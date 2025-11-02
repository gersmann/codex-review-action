.PHONY: help fmt lint type qa hooks hooks-run

PY_SRC := cli

help:
	@echo "Targets:"
	@echo "  fmt   - Format code (ruff for mat)"
	@echo "  lint  - Lint and autofix (ruff check --fix)"
	@echo "  type  - Type-check (mypy)"
	@echo "  qa    - Run fmt, lint, and type"
	@echo "  hooks - Install pre-commit hooks"
	@echo "  hooks-run - Run pre-commit on all files"

lint:
	uv run ruff format $(PY_SRC)
	uv run ruff check --fix $(PY_SRC)
	uv run mypy $(PY_SRC)

fmt:
	uv run ruff format $(PY_SRC)

type:
	uv run mypy $(PY_SRC)

qa: fmt lint type

hooks:
	uv run pre-commit install

hooks-run:
	uv run pre-commit run --all-files
