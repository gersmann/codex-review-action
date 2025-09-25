.PHONY: help fmt lint type qa

PY_SRC := cli

help:
	@echo "Targets:"
	@echo "  fmt   - Run ruff format"
	@echo "  lint  - Run ruff check --fix"
	@echo "  type  - Run mypy"
	@echo "  qa    - Run fmt, lint, and type"

fmt:
	uv run ruff format $(PY_SRC)

lint:
	uv run ruff check --fix $(PY_SRC)

type:
	uv run mypy $(PY_SRC)

qa: fmt lint type

