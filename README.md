Codex Autonomous Code Review (Reusable Action)

This reusable GitHub Action runs the Codex agent to review a pull request using your review guidelines, then posts a summary and inline review comments via the GitHub API.

Quick start

- In your repository, add a workflow like (using the `latest` tag):

  name: Codex Review
  on:
    pull_request:
      types: [opened, synchronize, reopened, ready_for_review]
  permissions:
    contents: read
    pull-requests: write
  jobs:
    review:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
          with:
            fetch-depth: 0
        - name: Codex autonomous review
          uses: gersmann/codex-review-action@latest
          with:
            openai_api_key: ${{ secrets.OPENAI_API_KEY }}
            model: gpt-5
            reasoning_effort: medium
            debug_level: 1

- Or pin to the stable major tag:

  name: Codex Review
  on:
    pull_request:
      types: [opened, synchronize, reopened, ready_for_review]
  permissions:
    contents: read
    pull-requests: write
  jobs:
    review:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
          with:
            fetch-depth: 0
        - name: Codex autonomous review
          uses: gersmann/codex-review-action@v1
          with:
            openai_api_key: ${{ secrets.OPENAI_API_KEY }}
            model: gpt-5
            reasoning_effort: medium
            debug_level: 1

Prompt options

- By default (prompt_strategy: auto), the action looks for an inline prompt, then a file, then falls back to a built-in prompt bundled with the action.
- Override via inputs:
  - prompt_strategy: auto | builtin | file | inline (default auto)
  - prompt_path: path to a prompt file in the repo (default prompts/code-review.md)
  - prompt_inline: supply the full prompt text directly

Requirements

- An OpenAI API key (set as repository secret OPENAI_API_KEY and pass via input openai_api_key).
- The runner’s system Python (ubuntu-latest is fine). The action installs codex-python from PyPI at runtime.

Inputs

- openai_api_key (string, required): OpenAI API key.
- model (string, default gpt-5): Model to use (e.g., gpt-5, gpt-4o-mini).
- reasoning_effort (string, default medium): minimal | low | medium | high.
- debug_level (string, default 0): 0 (off), 1 (basic), 2 (trace HTTP + anchoring).
- dry_run (string, default 0): if 1, prints payloads but does not post comments.
- stream_agent_messages (string, default 1): if 1, streams agent output to logs.
- codex_python_version (string, default empty): Version specifier passed to pip (e.g., ">=0.2.3").
- extra_pip_args (string, default empty): Extra pip flags (e.g., --index-url …).

What it posts

- A summary review on the PR with overall verdict and explanation.
- Inline comments for each finding with precise file/line anchoring when the line exists in the diff.
- If a referenced line is not in the diff, a file-level comment is posted instead.

Troubleshooting

- 422 Unprocessable Entity on comments: The action uses diff positions (not line/side). If a line isn’t in the diff, it falls back to a file-level comment.
- Model errors (builder error): Ensure model input is valid for your key; try model: gpt-5.
- No prompt file: Ensure prompts/code-review.md exists in the target repository.

Notes

- This action uses system Python and installs codex-python wheels from PyPI. If the runner version lacks a compatible wheel, install failures will occur; pin a version with known wheels using codex_python_version if needed.

Edit Commands (Comment-Triggered)

- You can ask the agent to make focused code edits by commenting on the PR with an @codex command. Supported triggers at the start of a comment:
  - `@codex ...`
  - `@codex: ...`
  - `@codex edit: ...`
  - `/codex ...`
- The remainder of the comment becomes the instruction for the coding agent. The agent:
  - Runs with plan + apply_patch tools enabled and AUTO approvals (no manual confirmations).
  - Applies minimal diffs, updates docs/tests as needed.
  - Commits with a message like `Codex edit: <first line>` and pushes to the PR head branch.
  - In dry-run mode, prints intended changes but does not commit/push.

Required workflow events and permissions

Add comment-based triggers and write permissions to your workflow file using this action:

```
name: Codex Review & Edits
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created]
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created]
permissions:
  contents: write         # allow commits/pushes
  pull-requests: write    # allow posting comments/reviews
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          # Ensure we can push back to the PR head branch
          fetch-depth: 0
      - name: Codex autonomous review & edits
        uses: gersmann/codex-review-action@v1
        with:
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5-mini
          reasoning_effort: medium
```

Notes and limitations for edits

- The action pushes to the PR head branch using `GITHUB_TOKEN`. For forked PRs, GitHub may block pushing from the base repository workflow; prefer running the workflow within the fork or grant appropriate permissions.
- The agent writes only within the checked-out workspace. Large refactors should be split into multiple commands.
- To preview changes without pushing, set input `dry_run: '1'` or comment with instructions and then re-run with dry-run disabled.
