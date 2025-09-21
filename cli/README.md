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
- Explicit mode selection (review vs act)

## Usage

### As a CLI Tool

```bash
# Review a specific PR (default mode)
python -m cli.main --repo owner/repo --pr 123

# Explicit review mode
python -m cli.main --repo owner/repo --pr 123 --mode review

# Act mode with custom instructions
python -m cli.main --repo owner/repo --pr 123 --mode act --act-instructions "Run tests after changes"

# Dry run mode
python -m cli.main --repo owner/repo --pr 123 --dry-run

# Debug mode
python -m cli.main --repo owner/repo --pr 123 --debug 2
```

### GitHub Actions

When used via the composite action, the CLI runs in GitHub Actions mode automatically and reads the event payload to determine whether to run a full review or a comment-triggered edit.

Comment-triggered edits

- Add a comment on the PR that starts with:
  - `/codex <instructions>` or `/codex: <instructions>`
- The remainder of the comment is passed to the coding agent. The agent runs with plan + apply_patch enabled and AUTO approvals, commits, and pushes changes to the PR head branch (unless dry-run).

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GITHUB_TOKEN` | GitHub API token | *Required* |
| `OPENAI_API_KEY` | OpenAI API key | *Required for OpenAI* |
| `CODEX_MODE` | Operation mode (review/act) | `review` |
| `CODEX_MODEL` | Model name | `gpt-5` |
| `CODEX_PROVIDER` | Model provider | `openai` |
| `CODEX_REASONING_EFFORT` | Reasoning effort level | `medium` |
| `CODEX_FAST_MODEL` | Fast model for dedup on repeated runs | `gpt-5-mini` |
| `CODEX_FAST_REASONING_EFFORT` | Reasoning effort for fast model | `low` |
| `CODEX_ACT_INSTRUCTIONS` | Additional instructions for act mode | `` |
| `DEBUG_CODEREVIEW` | Debug level (0-2) | `0` |
| `DRY_RUN` | Skip posting (1 for dry run) | `0` |

## Operation Modes

- **`review`** (default): Analyzes PR diffs using built-in guidelines from `prompts/review.md`
- **`act`**: Responds to `/codex` commands in PR comments to make autonomous code edits with optional custom instructions

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
- When detected, it now performs two layers of dedupe:
  - A strict prefilter that drops any new finding if an inline comment already exists on the same file within a few lines (covers resolved threads as well; we do not re‑post resolved items).
  - A fast‑model semantic pass to cull remaining near‑duplicates. You will see lines like:
    - `Prefilter dropped 2/5 findings due to existing comments`
    - `Dedup kept 3/5 findings (fast model)`
- Configure the semantic pass via flags or env:
  - `--fast-model`, `--fast-reasoning-effort`
  - `CODEX_FAST_MODEL`, `CODEX_FAST_REASONING_EFFORT`

### Customizing the Review Prompt

- Provide extra reviewer guidance using env `CODEX_ADDITIONAL_PROMPT` (verbatim text). When set, it is appended after the built-in guidelines and before the line-selection rules.

## Benefits

1. **Maintainable**: Clear separation of concerns makes code easier to maintain
2. **Testable**: Each component can be unit tested independently
3. **Extensible**: Easy to add new features or modify existing ones
4. **Flexible**: Works as CLI tool or GitHub Action
5. **Robust**: Better error handling and validation
6. **Debuggable**: Structured logging and debug levels
