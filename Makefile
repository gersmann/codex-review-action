.PHONY: help fmt lint type qa

PY_SRC := cli scripts

help:
	@echo "Targets:"
	@echo "  fmt   - Run ruff format"
	@echo "  lint  - Run ruff check --fix"
	@echo "  type  - Run mypy"
	@echo "  qa    - Run fmt, lint, and type"

fmt:
	uvx ruff format $(PY_SRC)

lint:
	uvx ruff check --fix $(PY_SRC)

type:
	uvx mypy $(PY_SRC)

qa: fmt lint type

