# Repository Guidelines

## Project Structure & Module Organization
- `cli/`: Python source. Entry point `main.py`; config/models in `config.py`, `models.py`, `exceptions.py`; workflow in `workflows/review_workflow.py`; prompt composition in `review/review_prompt.py`; infrastructure in `codex_client.py`, `github_client.py`, `git_ops.py`; review helpers in `review/dedupe.py`, `review/posting.py`; diff utilities in `patch_parser.py`, `anchor_engine.py`; context in `context_manager.py`.
- `prompts/`: Review guidelines (`review.md`).
- `action.yml`: Composite GitHub Action definition and inputs.
- `Makefile`: QA tasks (`fmt`, `lint`, `type`, `qa`).
- `README.md`, `cli/README.md`: Usage and architecture notes.

## Build, Test, and Development Commands
- `make lint` – Ruff lint + autofix.
- Run locally: `GITHUB_TOKEN=... OPENAI_API_KEY=... PYTHONPATH=. python -m cli.main --repo owner/repo --pr 123 [--dry-run] [--debug 1]`.

## Coding Style & Naming Conventions
- Python 3.12, 4‑space indent, `snake_case` for functions/variables, `PascalCase` for classes, constants `UPPER_SNAKE`.
- Keep functions focused and small; prefer pure helpers in `cli/` modules.
- Formatting and linting via Ruff; type hints required enough to pass MyPy (third‑party imports are ignored per `mypy.ini`).

## Testing Guidelines
- Tests use `pytest`. Run with `make test` or `pytest tests/`.
- Place tests under `tests/`; name files `test_*.py`; target modules in `cli/`.
- Mock GitHub Actions runs by writing a minimal event JSON and pointing `GITHUB_EVENT_PATH` to it; set `GITHUB_TOKEN` and `OPENAI_API_KEY` to test tokens.

## Commit & Pull Request Guidelines
- Never commit directly to `main`.
- Commit subjects use imperative mood ("Add X", not "Added/Adds"), are
  capitalized, have no trailing period, are at most 50 characters, and have no
  prefixes such as `feat:` or `fix:`.
- Commit bodies are separated from the subject by a blank line, wrapped at 72
  characters, and explain what changed and why rather than how. Skip the body
  when the subject is self-explanatory.
- Keep one logical change per commit. If the subject needs "and", split it.
- Use a concise, imperative PR title that describes the outcome rather than a
  category of work or a mechanical list of files changed.
- PR summaries explain the motivation and impact: why the change was made and
  what it enables. Do not mechanically list changes already visible in the
  diff.
- Do not include a "Test Plan" section.
- Add screenshots, sample payloads, or before/after output when changes affect
  external integrations or responses. Update docs if flags or inputs change.
- Pre-submit: `make qa` must pass; keep diffs minimal and scoped.

## Security & Configuration Tips
- Never print secrets; avoid high `--debug` on public logs. Prefer `--dry-run` when exploring.
- Default model/config are set in `action.yml`; override via inputs or env (`CODEX_*`).

## Agent-Specific Instructions
- When changing CLI args or Action inputs, update `README.md` and examples.
- Clean execution flow, fail fast, handle errors at a higher level, reraising wrapped exceptions doesn't add value. 
- No getattr, this is a typed codebase. 
