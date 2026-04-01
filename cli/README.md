# Codex Code Review CLI

A modular, well-structured CLI for autonomous code review using Codex.

## Overview

This CLI provides autonomous code review capabilities for GitHub pull requests. It has been refactored from the original monolithic script into a clean, modular architecture with proper separation of concerns.

## Architecture

```
cli/
├── __init__.py
├── main.py                    # CLI entry point with argparse
├── core/
│   ├── __init__.py
│   ├── config.py              # Configuration loading + validation
│   ├── models.py              # Typed dataclasses (comments, findings, payloads)
│   ├── exceptions.py          # Custom exception hierarchy
│   └── github_types.py        # Shared typed protocols for GitHub objects
├── clients/
│   ├── __init__.py
│   ├── codex_client.py        # Codex SDK wrapper for streaming + parsing
│   ├── codex_event_debugger.py # Protocol event debug formatting helpers
│   ├── github_client.py       # GitHub API helpers (PyGithub wrapper)
│   └── git_ops.py             # Git subprocess helpers
├── review/
│   ├── __init__.py
│   ├── artifacts.py           # Review artifact persistence + summary rendering
│   ├── dedupe.py              # Duplicate-detection helpers
│   ├── posting.py             # Inline comment payload/build/post helpers
│   ├── review_prompt.py       # Review prompt composition
│   ├── context_manager.py     # Context artifact writing
│   ├── patch_parser.py        # Patch parsing utilities
│   └── anchor_engine.py       # Diff anchor resolution utilities
├── workflows/
│   ├── __init__.py
│   ├── edit_prompt.py         # ACT prompt/context formatting helpers
│   ├── review_workflow.py     # Review orchestration
│   └── edit_workflow.py       # ACT mode orchestration
```

## Key Improvements

### 1. **Modular Design**
- **GitHub API**: PyGithub is wrapped in `clients/github_client.py`; review orchestration lives in `workflows/review_workflow.py`
- **Codex API**: `codex-python` SDK is wrapped in `clients/codex_client.py`
- **Patch Processing**: `review/patch_parser.py` contains utilities for parsing unified diffs
- **Configuration**: `ReviewConfig` centralizes all configuration management
- **Prompt Building**: `review/review_prompt.py` provides guideline loading and prompt composition helpers
- **Review Processing**: `ReviewWorkflow` orchestrates the entire review workflow

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

Review posting behavior:
- Codex posts a PR-level issue comment with a review summary.
- Findings are posted as standalone inline PR review comments on the relevant lines.

Comment-triggered edits

- Add a comment on the PR that starts with:
  - `/codex <instructions>` or `/codex: <instructions>`
- Bare `/codex` comments are ignored; the command must include instructions.
- The remainder of the comment is passed to the coding agent. The workflow executes the agent with the configured Codex runtime settings for this environment, then commits and pushes resulting branch changes (unless dry-run).

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GITHUB_TOKEN` | GitHub API token | *Required* |
| `OPENAI_API_KEY` | OpenAI API key | *Required for OpenAI* |
| `CODEX_MODE` | Operation mode (review/act) | `review` |
| `CODEX_MODEL` | Model name | `gpt-5.4` |
| `CODEX_PROVIDER` | Model provider | `openai` |
| `CODEX_REASONING_EFFORT` | Reasoning effort level | `medium` |
| `CODEX_ACT_INSTRUCTIONS` | Additional instructions for act mode | `` |
| `CODEX_ALLOWED_COMMENTER_ASSOCIATIONS` | Comma-separated GitHub comment roles allowed to trigger act mode | `MEMBER,OWNER,COLLABORATOR` |
| `DEBUG_CODEREVIEW` | Debug level (0-2) | `0` |
| `DRY_RUN` | Skip posting (1 for dry run) | `0` |

Invalid `CODEX_ALLOWED_COMMENTER_ASSOCIATIONS` values fail fast during configuration loading.

## Operation Modes

- **`review`** (default): Analyzes PR diffs using built-in guidelines from `prompts/review.md`
- **`act`**: Responds to `/codex` commands in PR comments to make autonomous code edits with optional custom instructions

## Testing

Each module can be tested independently:

```bash
pytest tests/ -v
```

## Deduplication on Repeated Runs

- The CLI detects if a prior Codex review exists on the PR (looks for a summary containing "Codex Autonomous Review:" or earlier inline review comments).
- When detected, deduplication happens in three layers:
  - **Codex-thread attribution**: only unresolved review threads whose root author matches a prior Codex summary author are reused as rerun context.
  - **Inline semantic dedup**: the structured-output turn uses those prior Codex comments to decide which issues are new vs already covered.
  - **Re-adjudicated summary carry-forward**: the model returns prior comment IDs that still seem relevant, and the summary reports those separately from new findings.
  - **Auto-resolution of fixed Codex threads**: the model can also mark prior unresolved Codex comments as fixed, and review mode resolves those GitHub review threads automatically.

## Review Resume Between Pushes

- Review mode can resume the previous Codex thread when a PR receives new commits.
- The summary issue comment stores the last reviewed head SHA in hidden metadata.
- GitHub Actions review runs restore an isolated review-only `CODEX_HOME` cache keyed by repository, PR number, model, and reviewed SHA.
- When the prior reviewed SHA is still an ancestor of the current head and the cached session index contains a thread, the workflow resumes that thread and narrows the prompt to `previous_reviewed_sha..HEAD`.
- Small incremental diffs are embedded directly in the prompt; larger deltas are referenced by commit range and inspected with git during the review turn.

### Customizing the Review Prompt

- Provide extra reviewer guidance using env `CODEX_ADDITIONAL_PROMPT` (verbatim text). When set, it is appended after the built-in guidelines and before the line-selection rules.

## Benefits

1. **Maintainable**: Clear separation of concerns makes code easier to maintain
2. **Testable**: Each component can be unit tested independently
3. **Extensible**: Easy to add new features or modify existing ones
4. **Flexible**: Works as CLI tool or GitHub Action
5. **Robust**: Better error handling and validation
6. **Debuggable**: Structured logging and debug levels
