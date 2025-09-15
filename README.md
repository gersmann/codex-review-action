# Codex Code Review & Actor

This reusable GitHub Action runs the Codex agent to review a pull request using built-in review guidelines, then posts a summary and precise inline review comments using the GitHub API.

Note: Never commit API keys or other secrets to the repository. Provide credentials via GitHub Secrets and pass them through action inputs or environment variables as documented below.

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

- An OpenAI API key (set as repository secret OPENAI_API_KEY and pass via input openai_api_key).
- The runner’s system Python (ubuntu-latest is fine). The action installs codex-python from PyPI at runtime.

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
- codex_python_version: pip version specifier for codex-python
- extra_pip_args: additional pip flags
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

- This action uses system Python and installs codex-python wheels from PyPI. If the runner version lacks a compatible wheel, install failures will occur; pin a version with known wheels using codex_python_version if needed.

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
