# Repository Guidelines

## Project Structure & Module Organization

- `cli/`: Python source. Entry point `main.py`; config/models in `config.py`, `models.py`, `exceptions.py`; workflows in `workflows/review_workflow.py`, `workflows/edit_workflow.py`; prompts in `review_prompt.py`, `edit_prompt.py`; infrastructure in `codex_client.py`, `github_client.py`, `git_ops.py`; review helpers in `review/dedupe.py`, `review/posting.py`; diff utilities in `patch_parser.py`, `anchor_engine.py`; context in `context_manager.py`.
- `prompts/`: Review guidelines (`review.md`).
- `action.yml`: Composite GitHub Action definition and inputs.
- `Makefile`: QA tasks (`fmt`, `lint`, `type`, `qa`).
- `README.md`, `cli/README.md`: Usage and architecture notes.

## Build, Test, and Development Commands

- `make lint` – Ruff lint + autofix.
- Run locally: `GITHUB_TOKEN=... OPENAI_API_KEY=... PYTHONPATH=. python -m cli.main --repo owner/repo --pr 123 [--mode review|act] [--dry-run] [--debug 1]`.

## Coding Style & Naming Conventions

- Python 3.14, 4‑space indent, `snake_case` for functions/variables, `PascalCase` for classes, constants `UPPER_SNAKE`.
- Keep functions focused and small; prefer pure helpers in `cli/` modules.
- Formatting and linting via Ruff; type hints required enough to pass MyPy (third‑party imports are ignored per `mypy.ini`).

## Testing Guidelines

- Tests use `pytest`. Run with `make test` or `pytest tests/`.
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

- When changing CLI args or Action inputs, update `README.md` and examples.
- Clean execution flow, fail fast, handle errors at a higher level, reraising wrapped exceptions doesn't add value.
- No getattr, this is a typed codebase.
