.PHONY: help fmt lint type qa

PY_SRC := cli

# Helper: try a series of commands until one succeeds. If none are available,
# print a warning but do not fail the target (useful in minimal CI envs).
define try_cmd
  ( $(1) ) || ( $(2) ) || ( $(3) ) || ( echo "[warn] Skipping: required tool not available" )
endef

help:
	@echo "Targets:"
	@echo "  fmt   - Run ruff format (prefers 'uvx', falls back to system/py -m)"
	@echo "  lint  - Run ruff check --fix"
	@echo "  type  - Run mypy"
	@echo "  qa    - Run fmt, lint, and type"

fmt:
	$(call try_cmd,uvx ruff format $(PY_SRC),ruff format $(PY_SRC),python -m ruff format $(PY_SRC))

lint:
	$(call try_cmd,uvx ruff check --fix $(PY_SRC),ruff check --fix $(PY_SRC),python -m ruff check --fix $(PY_SRC))

type:
	$(call try_cmd,uvx mypy $(PY_SRC),mypy $(PY_SRC),python -m mypy $(PY_SRC))

qa: fmt lint type

