.PHONY: help fmt lint type qa hooks hooks-run requirements check-requirements

PY_SRC := cli
UV_EXPORT := uv export --locked --no-dev --no-emit-project --no-hashes --no-header

help:
	@echo "Targets:"
	@echo "  fmt   - Format code (ruff for mat)"
	@echo "  lint  - Lint and autofix (ruff check --fix)"
	@echo "  type  - Type-check (mypy)"
	@echo "  qa    - Run fmt, lint, and type"
	@echo "  hooks - Install pre-commit hooks"
	@echo "  hooks-run - Run pre-commit on all files"
	@echo "  requirements - Export locked runtime dependencies for the Action"
	@echo "  check-requirements - Check that the runtime export is current"

requirements:
	$(UV_EXPORT) --output-file requirements.txt

check-requirements:
	$(UV_EXPORT) | diff -u requirements.txt -

lint:
	uv run ruff format .
	uv run ruff check --fix .
	uv run mypy .

fmt:
	uv run ruff format $(PY_SRC)

type:
	uv run mypy $(PY_SRC)

qa: fmt lint type

hooks:
	uv run pre-commit install

hooks-run:
	uv run pre-commit run --all-files
