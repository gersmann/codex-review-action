# Codex Code Review CLI

The CLI analyzes a GitHub pull request and posts review findings. It has no repository-editing workflow.

## Architecture

```text
cli/
├── main.py                     # CLI and GitHub event entry point
├── core/
│   ├── config.py               # Review configuration and validation
│   ├── models.py               # Findings, threads, and posting payloads
│   ├── exceptions.py           # Domain exception hierarchy
│   └── github_types.py         # Typed GitHub protocols
├── clients/
│   ├── codex_client.py         # Codex SDK wrapper
│   ├── codex_event_debugger.py # Protocol event diagnostics
│   ├── github_client.py        # PyGithub wrapper
│   └── git_ops.py              # Read-only review diff helpers
├── review/
│   ├── artifacts.py            # Review context artifacts
│   ├── context_manager.py      # PR discussion snapshots
│   ├── dedupe.py               # Prior-finding attribution
│   ├── posting.py              # Inline comment posting
│   ├── review_prompt.py        # Prompt composition
│   ├── patch_parser.py         # Unified-diff parsing
│   └── anchor_engine.py        # Diff-line anchoring
└── workflows/
    └── review_workflow.py      # Review orchestration
```

## CLI Usage

```bash
python -m cli.main --repo owner/repo --pr 123
python -m cli.main --repo owner/repo --pr 123 --dry-run
python -m cli.main --repo owner/repo --pr 123 --debug 2
```

Defaults:

- Model: `gpt-5.6-luna`
- Reasoning effort: `high`
- Provider: `openai`

## GitHub Actions Events

The bundled workflow listens only for `issue_comment` and `pull_request_review_comment` events whose body begins with `/review` or `/verify`. Pull-request submission does not start a review.

Authorized comments may override one run:

```text
/review reasoning:xhigh model:gpt-5.6-sol
```

Unsupported commands and unauthorized commenters are ignored.
Authorized requests receive a rocket reaction and a queued reply before the
review starts.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GITHUB_TOKEN` | GitHub API token | Required |
| `OPENAI_API_KEY` | OpenAI API key | Required |
| `CODEX_MODEL` | `gpt-5.6-luna`, `gpt-5.6-terra`, or `gpt-5.6-sol` | `gpt-5.6-luna` |
| `CODEX_PROVIDER` | Model provider | `openai` |
| `CODEX_REASONING_EFFORT` | Reasoning effort | `high` |
| `CODEX_WEB_SEARCH_MODE` | Web search mode | `live` |
| `CODEX_ADDITIONAL_PROMPT` | Additional reviewer guidance | Empty |
| `CODEX_ALLOWED_COMMENTER_ASSOCIATIONS` | Roles allowed to request reviews | `MEMBER,OWNER,COLLABORATOR` |
| `DEBUG_CODEREVIEW` | Debug level from `0` to `2` | `0` |
| `DRY_RUN` | Skip comment posting when set to `1` | `0` |

Invalid configuration fails before the review starts.

## Review Flow

1. Load the PR, changed files, discussion snapshots, and unresolved Codex-authored threads.
2. Build review context and ask Codex for schema-validated findings.
3. Re-adjudicate prior findings separately from new findings.
4. Anchor new findings to current diff lines and post publishable comments.
5. Refresh the PR summary with active-finding counts, token usage, and estimated
   cost.

## Testing

```bash
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy .
```
