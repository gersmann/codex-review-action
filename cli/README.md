# Codex Code Review CLI

A modular, well-structured CLI for autonomous code review using Codex.

## Overview

This CLI provides autonomous code review capabilities for GitHub pull requests. It has been refactored from the original monolithic script into a clean, modular architecture with proper separation of concerns.

## Architecture

```
cli/
├── __init__.py
├── main.py                    # CLI entry point with argparse
├── config.py                  # Configuration management
├── exceptions.py              # Custom exception hierarchy
├── (no client wrapper)        # Direct PyGithub usage in review_processor.py
├── patch_parser.py            # Patch parsing utilities
├── prompt_builder.py          # Prompt composition and guidelines
└── review_processor.py        # Core review processing logic
```

## Key Improvements

### 1. **Modular Design**
- **GitHub API**: PyGithub is used directly in `review_processor.py`
- **Patch Processing**: `patch_parser.py` contains utilities for parsing unified diffs
- **Configuration**: `ReviewConfig` centralizes all configuration management
- **Prompt Building**: `PromptBuilder` handles guidelines loading and prompt composition
- **Review Processing**: `ReviewProcessor` orchestrates the entire review workflow

### 2. **Better Error Handling**
- Custom exception hierarchy instead of `sys.exit()` calls
- Structured error messages with context
- Graceful degradation for non-critical errors

### 3. **CLI Interface**
- Full command-line argument support using `argparse`
- Can be used as a standalone CLI or in GitHub Actions mode
- Comprehensive help and examples

### 4. **Configuration Management**
- Environment variable support with validation
- Command-line argument overrides
- Flexible guidelines loading strategies

## Usage

### As a CLI Tool

```bash
# Review a specific PR
python -m cli.main --repo owner/repo --pr 123

# Use custom guidelines
python -m cli.main --repo owner/repo --pr 123 --guidelines-file custom.md

# Dry run mode
python -m cli.main --repo owner/repo --pr 123 --dry-run

# Debug mode
python -m cli.main --repo owner/repo --pr 123 --debug 2
```

### GitHub Actions

When used via the composite action, the CLI runs in GitHub Actions mode automatically and reads the event payload to determine whether to run a full review or a comment-triggered edit.

Comment-triggered edits

- Add a comment on the PR that starts with one of:
  - `@codex ...`
  - `@codex: ...`
  - `@codex edit: ...`
  - `/codex ...`
- The remainder of the comment is passed to the coding agent. The agent runs with plan + apply_patch enabled and AUTO approvals, commits, and pushes changes to the PR head branch (unless dry-run).

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GITHUB_TOKEN` | GitHub API token | *Required* |
| `OPENAI_API_KEY` | OpenAI API key | *Required for OpenAI* |
| `CODEX_MODEL` | Model name | `gpt-5` |
| `CODEX_PROVIDER` | Model provider | `openai` |
| `CODEX_REASONING_EFFORT` | Reasoning effort level | `medium` |
| `CODEX_FAST_MODEL` | Fast model for dedup on repeated runs | `gpt-5-mini` |
| `CODEX_FAST_REASONING_EFFORT` | Reasoning effort for fast model | `low` |
| `REVIEW_PROMPT_STRATEGY` | Guidelines strategy | `auto` |
| `REVIEW_PROMPT_PATH` | Guidelines file path | `prompts/code-review.md` |
| `REVIEW_PROMPT_INLINE` | Inline guidelines text | `` |
| `DEBUG_CODEREVIEW` | Debug level (0-2) | `0` |
| `DRY_RUN` | Skip posting (1 for dry run) | `0` |

## Guidelines Strategies

- **`auto`**: Try inline → file → builtin (default)
- **`inline`**: Use `REVIEW_PROMPT_INLINE` environment variable
- **`file`**: Use file specified by `REVIEW_PROMPT_PATH`
- **`builtin`**: Use built-in guidelines from `prompts/review.md`

## Testing

Each module can be tested independently:

```python
# Test configuration
from cli.config import ReviewConfig
config = ReviewConfig.from_environment()

# PyGithub usage is internal to ReviewProcessor

# Test patch parsing
from cli.patch_parser import parse_valid_head_lines_from_patch
lines = parse_valid_head_lines_from_patch(patch_content)
```

## Deduplication on Repeated Runs

- The CLI detects if a prior Codex review exists on the PR (looks for a summary containing "Codex Autonomous Review:" or earlier inline review comments).
- When detected, it collects existing comment text and runs a fast-model pass to filter new findings. You will see a line like:
  - `Dedup kept 3/5 findings (fast model)`
- Configure via flags or env:
  - `--fast-model`, `--fast-reasoning-effort`
  - `CODEX_FAST_MODEL`, `CODEX_FAST_REASONING_EFFORT`

## Benefits

1. **Maintainable**: Clear separation of concerns makes code easier to maintain
2. **Testable**: Each component can be unit tested independently
3. **Extensible**: Easy to add new features or modify existing ones
4. **Flexible**: Works as CLI tool or GitHub Action
5. **Robust**: Better error handling and validation
6. **Debuggable**: Structured logging and debug levels
