# Repository Guidelines

## Project Structure & Module Organization
- `cli/`: Python source. Entry point `main.py`; core `review_processor.py`, `edit_processor.py`, `patch_parser.py`, `prompt_builder.py`, `config.py`, `exceptions.py`, `anchor_engine.py`.
- `prompts/`: Review guidelines (`review.md`).
- `action.yml`: Composite GitHub Action definition and inputs.
- `Makefile`: QA tasks (`fmt`, `lint`, `type`, `qa`).
- `README.md`, `cli/README.md`: Usage and architecture notes.

## Build, Test, and Development Commands
- `make lint` – Ruff lint + autofix.
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
- When changing CLI args or Action inputs, update `README.md` and examples.
- Clean execution flow, fail fast, handle errors at a higher level, reraising wrapped exceptions doesn't add value. 
- No getattr, this is a typed codebase. 
- 

## Issue Tracking with bd (beads)

**IMPORTANT**: This project uses **bd (beads)** for ALL issue tracking. Do NOT use markdown TODOs, task lists, or other tracking methods.

### Why bd?

- Dependency-aware: Track blockers and relationships between issues
- Git-friendly: Auto-syncs to JSONL for version control
- Agent-optimized: JSON output, ready work detection, discovered-from links
- Prevents duplicate tracking systems and confusion

### Quick Start

**Check for ready work:**
```bash
bd ready --json
```

**Create new issues:**
```bash
bd create "Issue title" -t bug|feature|task -p 0-4 --json
bd create "Issue title" -p 1 --deps discovered-from:bd-123 --json
```

**Claim and update:**
```bash
bd update bd-42 --status in_progress --json
bd update bd-42 --priority 1 --json
```

**Complete work:**
```bash
bd close bd-42 --reason "Completed" --json
```

### Issue Types

- `bug` - Something broken
- `feature` - New functionality
- `task` - Work item (tests, docs, refactoring)
- `epic` - Large feature with subtasks
- `chore` - Maintenance (dependencies, tooling)

### Priorities

- `0` - Critical (security, data loss, broken builds)
- `1` - High (major features, important bugs)
- `2` - Medium (default, nice-to-have)
- `3` - Low (polish, optimization)
- `4` - Backlog (future ideas)

### Workflow for AI Agents

1. **Check ready work**: `bd ready` shows unblocked issues
2. **Claim your task**: `bd update <id> --status in_progress`
3. **Work on it**: Implement, test, document
4. **Discover new work?** Create linked issue:
   - `bd create "Found bug" -p 1 --deps discovered-from:<parent-id>`
5. **Complete**: `bd close <id> --reason "Done"`
6. **Commit together**: Always commit the `.beads/issues.jsonl` file together with the code changes so issue state stays in sync with code state

### Auto-Sync

bd automatically syncs with git:
- Exports to `.beads/issues.jsonl` after changes (5s debounce)
- Imports from JSONL when newer (e.g., after `git pull`)
- No manual export/import needed!

### MCP Server (Recommended)

If using Claude or MCP-compatible clients, install the beads MCP server:

```bash
pip install beads-mcp
```

Add to MCP config (e.g., `~/.config/claude/config.json`):
```json
{
  "beads": {
    "command": "beads-mcp",
    "args": []
  }
}
```

Then use `mcp__beads__*` functions instead of CLI commands.

### Important Rules

- ✅ Use bd for ALL task tracking
- ✅ Always use `--json` flag for programmatic use
- ✅ Link discovered work with `discovered-from` dependencies
- ✅ Check `bd ready` before asking "what should I work on?"
- ❌ Do NOT create markdown TODO lists
- ❌ Do NOT use external issue trackers
- ❌ Do NOT duplicate tracking systems

For more details, see README.md and QUICKSTART.md.
