# Repository Guidelines

## Project Structure & Module Organization
- `cli/`: Python source. Entry point `main.py`; core `review_processor.py`, `edit_processor.py`, `patch_parser.py`, `prompt_builder.py`, `config.py`, `exceptions.py`, `anchor_engine.py`.
- `prompts/`: Review guidelines (`review.md`).
- `action.yml`: Composite GitHub Action definition and inputs.
- `Makefile`: QA tasks (`fmt`, `lint`, `type`, `qa`).
- `README.md`, `cli/README.md`: Usage and architecture notes.

## Build, Test, and Development Commands
- `make fmt` – format with Ruff. Requires `uv` (runs `uvx ruff format cli`).
- `make lint` – Ruff lint + autofix.
- `make type` – MyPy type-check (Python 3.12; see `mypy.ini`).
- `make qa` – run all of the above.
- Run locally: `GITHUB_TOKEN=... OPENAI_API_KEY=... PYTHONPATH=. python -m cli.main --repo owner/repo --pr 123 [--mode review|act] [--dry-run] [--debug 1]`.

## Coding Style & Naming Conventions
- Python 3.12, 4‑space indent, `snake_case` for functions/variables, `PascalCase` for classes, constants `UPPER_SNAKE`.
- Keep functions focused and small; prefer pure helpers in `cli/` modules.
- Formatting and linting via Ruff; type hints required enough to pass MyPy (third‑party imports are ignored per `mypy.ini`).

## Testing Guidelines
- No formal test suite yet. If adding tests, prefer `pytest`.
- Place tests under `tests/`; name files `test_*.py`; target modules in `cli/`.
- Mock GitHub Actions runs by writing a minimal event JSON and pointing `GITHUB_EVENT_PATH` to it; set `GITHUB_TOKEN` and `OPENAI_API_KEY` to test tokens.

## Commit & Pull Request Guidelines
- Use Conventional Commits (e.g., `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`) as seen in history.
- PRs: describe the what/why, link issues, include before/after output (CLI logs or posted comments). Update docs if flags/inputs change.
- Pre-submit: `make qa` must pass; keep diffs minimal and scoped.

## Security & Configuration Tips
- Never print secrets; avoid high `--debug` on public logs. Prefer `--dry-run` when exploring.
- Default model/config are set in `action.yml`; override via inputs or env (`CODEX_*`).

## Agent-Specific Instructions
- Apply minimal diffs; follow existing structure and style.
- When changing CLI args or Action inputs, update `README.md` and examples.
