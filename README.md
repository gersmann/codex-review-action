# Codex Code Review & Actor

This reusable GitHub Action runs the Codex agent to review a pull request using built-in review guidelines, then posts a summary and precise inline review comments using the GitHub API.


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

Operation Modes

This action supports two distinct modes:

- **review** (default): Analyzes PR diffs and posts review comments using built-in review guidelines
- **act**: Responds to `/codex` commands in PR comments to make autonomous code edits

Set the mode via the `mode` input parameter.

## Review Mode Example

For traditional code review on PR events:

```yaml
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
          mode: review  # explicit (though this is the default)
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5
          reasoning_effort: medium
```

## Act Mode Example

For autonomous code editing via `/codex` comments. The job only runs when a new comment contains `/codex`.

```yaml
name: Codex Review & Edits
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
permissions:
  contents: write         # allow commits/pushes
  pull-requests: write    # allow posting comments/reviews
jobs:
  act:
    name: Act on /codex comments
    if: >-
      (github.event_name == 'issue_comment' && contains(github.event.comment.body, '/codex')) ||
      (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '/codex'))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Codex autonomous edits
        uses: gersmann/codex-review-action@latest
        with:
          mode: act
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5
          # Optional: additional instructions appended to the edit prompt
          # act_instructions: "Keep diffs minimal and update tests"
```

Requirements

- An OpenAI API key (set as repository secret `OPENAI_API_KEY` and pass via input `openai_api_key`).
- Python 3.12+ on the runner (ubuntu-latest is fine). Dependencies are installed via `pip -r requirements.txt` bundled with the action.

Inputs

- mode: review | act (default: review)
- openai_api_key: OpenAI API key (required)
- model: e.g., gpt-5 (default: gpt-5)
- reasoning_effort: minimal | low | medium | high (default: medium)
- debug_level: 0 | 1 | 2 (default: 0)
- dry_run: '0' | '1' (default: '0')
- stream_agent_messages: '0' | '1' (default: '1')
- fast_model: e.g., gpt-5-mini (review mode only)
- fast_reasoning_effort: low | medium | high
- act_instructions: additional instructions in act mode
- codex_python_version: deprecated; install now uses the action’s requirements.txt and ignores this input
- extra_pip_args: additional pip flags (e.g., private index)
- include_annotated: true|false; include annotated diffs with HEAD line numbers in the model prompt (default: true)

What it posts

- A summary review on the PR with overall verdict and explanation.
- Inline comments for each finding using the single-comment API with line/side anchoring. Multi-line comments (with suggestions) are only posted when the selected range is contiguous in the same hunk and ≤ 5 lines. Otherwise a precise single-line comment is posted and any suggestion is rendered as a non-applicable diff block.
- When a requested line is not present in the diff, the finding is skipped (no file-level fallbacks).

Troubleshooting

- 422 Unprocessable Entity: Usually indicates the target line(s) are not present in the PR diff for the head commit. The action uses line/side anchoring and only posts when the target exists in the diff. Set `debug_level: 2` to see proposed anchors in logs.
- **Model errors (builder error)**: Ensure model input is valid for your key; try model: gpt-5.
- Review mode: Uses built-in review guidelines from prompts/review.md.

Notes

- The action installs runtime dependencies from its own `requirements.txt` (`codex-python`, `PyGithub`). You can supply custom `--index-url` or similar via `extra_pip_args`.

## Local Development with uv

This repo is not an installable Python package; it’s a GitHub Action with a CLI. The `pyproject.toml` uses a tool.uv-only configuration (`package = false`).

- Install uv: see https://docs.astral.sh/uv/
- Create/sync the environment:
  - `uv sync`  # installs deps declared under `[tool.uv]`

- What gets installed locally
  - Runtime deps for working with the CLI: `codex-python`, `PyGithub`
  - Dev tools: `ruff`, `mypy`, `pytest`

- QA helpers:
  - `make fmt`  # uvx ruff format cli
  - `make lint` # ruff check --fix
  - `make type` # mypy
  - `make qa`   # runs all


Note: If uv/ruff/mypy are not available in your environment, `make` targets will gracefully skip those steps instead of failing.

- Run locally:
  - `GITHUB_TOKEN=... OPENAI_API_KEY=... PYTHONPATH=. python -m cli.main --repo owner/repo --pr 123 [--mode review|act] [--dry-run] [--debug 1]`

Edit Commands (Comment-Triggered)

- You can ask the agent to make focused code edits by commenting on the PR with a `/codex` command. Supported forms:
  - `/codex <instructions>`
  - `/codex: <instructions>`
- The remainder of the comment becomes the instruction for the coding agent. The agent:
  - Runs with plan + apply_patch tools enabled and AUTO approvals (no manual confirmations).
  - Applies minimal diffs, updates docs/tests as needed.
  - Commits with a message like `Codex edit: <first line>` and pushes to the PR head branch.
  - In dry-run mode, prints intended changes but does not commit/push.

Pushing in act mode

- Ensure your workflow uses `actions/checkout@v4` with credentials persisted (the default) and grants `contents: write` permissions. Example:

  permissions:
    contents: write
    pull-requests: write
  steps:
    - uses: actions/checkout@v4
      with:
        fetch-depth: 0

- For pull requests from forks, the default `GITHUB_TOKEN` cannot push to the fork. In that case, either:
  - Run the edit job on branches in the main repo (not on fork PRs), or
  - Use a PAT with access to the fork (review security implications), or
  - Have the bot open a new branch and PR in the base repo (not yet supported by this action).

- The action will now push even when the worktree is clean but the local HEAD has unpushed commits (e.g., when a previous step created a commit). This avoids the "No changes to commit" early-exit preventing a push.

Required workflow events and permissions

Add comment-based triggers and write permissions to your workflow file using this action:

```
name: Codex Review & Edits
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created]
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
## Security: where to put the OpenAI API key

You should never commit API keys to the repo. Store them as encrypted GitHub Secrets and pass them to the action at runtime.

- Repository secret (recommended for most cases)
  1. In your repo, go to Settings → Secrets and variables → Actions → New repository secret.
  2. Name it `OPENAI_API_KEY` and paste your key.
  3. Reference it in the workflow: `openai_api_key: ${{ secrets.OPENAI_API_KEY }}`.

- Environment secrets with protections (recommended for “prod” usage)
  1. Create an Environment (e.g., `production`) under Settings → Environments.
  2. Add a secret named `OPENAI_API_KEY` there and optionally require reviewers.
  3. Use it in your job with `environment: production` and the same reference.

- Organization secrets (for many repos)
  - Define an org-level secret scoped to specific repositories and reference it the same way.

Additional tips

- Private repos are fine: secrets are encrypted and only exposed to your workflow at runtime. They are masked in logs by GitHub.
- Secrets are not passed to workflows triggered by pull_request events from forks unless you explicitly opt in. Avoid exposing secrets to untrusted code.
- For local runs, export the key in your shell instead of committing a file:
  - `export OPENAI_API_KEY=...` then run the CLI.
  - Add `.env` files to `.gitignore` if you use them locally.
- If a key was ever committed, rotate it immediately in your provider dashboard and update the GitHub secret.

Passing the key via env instead of input

This action also supports reading `OPENAI_API_KEY` from the job environment if you prefer:

```
jobs:
  review:
    runs-on: ubuntu-latest
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: gersmann/codex-review-action@v1
        with:
          # omit openai_api_key; the action will use env.OPENAI_API_KEY
          model: gpt-5
```
