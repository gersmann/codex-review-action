# Codex Review Action (Review + Act)

Run Codex to review pull requests and, on demand, make autonomous edits driven by “/codex …” comments.

- Review: posts precise inline review comments and a PR-level timeline summary as an issue comment. When there are no findings, only the timeline summary is posted.
- Act: applies focused edits when trusted users comment /codex; can run tests and services before pushing.

## Quick Start (Review)

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
        uses: gersmann/codex-review-action@v1
        with:
          mode: review
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5
          reasoning_effort: medium
          debug_level: 1
```

## Act on “/codex” Comments (with tests and services)

The example below mirrors a production setup where the project is checked out and prepared so Act can run the test suite before pushing edits.

```yaml
name: Codex Review & Act
on:
  issue_comment: { types: [created] }
  pull_request_review_comment: { types: [created] }
permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write
concurrency:
  group: codex-act-${{ github.event.issue.number || github.event.pull_request.number || github.ref }}
  cancel-in-progress: false
jobs:
  act:
    name: Act on /codex comments
    if: >-
      (
        github.event_name == 'issue_comment' &&
        startsWith(github.event.comment.body, '/codex') &&
        github.event.issue.pull_request &&
        contains(fromJSON('["MEMBER","OWNER","COLLABORATOR"]'), github.event.comment.author_association)
      ) || (
        github.event_name == 'pull_request_review_comment' &&
        startsWith(github.event.comment.body, '/codex') &&
        contains(fromJSON('["MEMBER","OWNER","COLLABORATOR"]'), github.event.comment.author_association)
      )
      && github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    services:
      mongodb:
        image: mongo:6
        ports: ["27017:27017"]
      postgres:
        image: postgres:14
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: --tmpfs /var/lib/postgresql/data
      redis:
        image: redis
        ports: ["6379:6379"]
    env:
      APP_ENV: test
      CI: true
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}
          token: ${{ secrets.REPO_ACCESS_TOKEN }}
      - name: Setup Environment
        # Project-specific example: this repo uses a local setup action.
        # Replace with your own steps (e.g., install deps, build artifacts, run DB migrations).
        uses: ./.github/actions/setup
        with:
          python-version: '3.13'
          node-version: '20'
      - name: Codex autonomous edits
        if: env.OPENAI_API_KEY != ''
        uses: gersmann/codex-review-action@v1
        with:
          mode: act
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5
          debug_level: 1
```

How “/codex” Commands Work
- `/codex <instructions>`: propose and apply minimal diffs in-scope.
- `/codex address comments` (or natural variants like “please fix the review comments”): address unresolved review threads using the full thread conversation as context. Only unresolved threads are considered; resolved threads are ignored. Edits are restricted to files referenced by those threads unless strictly necessary.
- `/codex focus <path>`: limit scope to a path.
- `/codex redo`: re-run on latest PR head.

## Inputs (key ones)

- mode: review | act (default: review)
- openai_api_key: OpenAI API key (required)
- model: e.g., gpt-5 (default: gpt-5)
- reasoning_effort: minimal | low | medium | high (default: medium)
- fast_model (review only): e.g., gpt-5-mini
- fast_reasoning_effort (review only): low | medium | high
- debug_level: 0 | 1 | 2 (default: 0)
- stream_agent_messages: '0' | '1' (default: '1')
- dry_run (act only): '0' | '1' (default: '0')
- act_instructions (act): extra guidance appended to edit prompt
- additional_prompt (review): extra reviewer instructions (verbatim)
- extra_pip_args: additional pip flags (e.g., private index)

## What It Posts

- Inline review comments anchored to exact diff lines; if a line isn’t in the current diff, it’s skipped.
- A PR-level timeline summary as an issue comment on each run (or only the summary when there are zero inline findings).
- Multi‑line suggestions only when contiguous and short; otherwise a precise single‑line comment.

## Security & Permissions

- Restrict Act triggers to trusted roles via author_association as shown.
- For forks, default GITHUB_TOKEN generally cannot push to the fork:
  - Run Act only on branches in the main repo, or
  - Use a PAT with fork access (weigh risk), or
  - Bot opens a new branch/PR in base repo (not currently implemented).
- Grant only what’s needed: contents: write (push), pull-requests: write (reviews), issues: write (required for timeline summary and ACT replies).

## Troubleshooting

- 422 Unprocessable Entity: target line not present in PR head diff. Rebase and re‑run; set `debug_level: 2` to log anchors.
- Model errors: ensure your key supports the selected model.
- Review uses built‑in prompts (see `prompts/review.md`).

## Release & Versioning

- Release Please opens/updates a release PR on pushes to `main` and builds notes from commits (including direct commits).
- Merge the release PR to create a GitHub release and semver tag (e.g., `v1.2.3`).
- After publish, `.github/workflows/release-published.yml` updates the `v1` and `latest` tags to the release commit when the major is `v1`.

## Local Development

This action is a Python CLI, not a library.

- uv workflow: `uv sync`
- QA: `make lint` (formats, lints with autofix, and type-checks)
- Local run: `GITHUB_TOKEN=… OPENAI_API_KEY=… PYTHONPATH=. python -m cli.main --repo owner/repo --pr 123 --mode review`
