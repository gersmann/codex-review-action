# Codex Review Action (Review + Act)

Run Codex to review pull requests and, on demand, make autonomous edits driven by `/codex` comments.

- **Review**: posts precise inline review comments and a PR-level summary. When there are no findings, only the summary is posted.
- **Act**: applies focused edits when trusted users comment `/codex`; commits and pushes to the PR branch.

## Quick Start (Review)

```yaml
name: Codex Review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions:
  contents: read
  pull-requests: write
  issues: write
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
```

## Act on `/codex` Comments

When a trusted user comments `/codex <instructions>` on a PR, the action checks out the branch, runs the coding agent, and pushes the result. Give the agent a working environment so it can run tests before pushing.

```yaml
name: Codex Act
on:
  issue_comment: { types: [created] }
  pull_request_review_comment: { types: [created] }
permissions:
  contents: write
  pull-requests: write
  issues: write
concurrency:
  group: codex-act-${{ github.event.issue.number || github.event.pull_request.number || github.ref }}
  cancel-in-progress: false
jobs:
  act:
    name: Act on /codex comments
    if: >-
      (
        (
          github.event_name == 'issue_comment' &&
          startsWith(github.event.comment.body, '/codex') &&
          github.event.issue.pull_request
        ) || (
          github.event_name == 'pull_request_review_comment' &&
          startsWith(github.event.comment.body, '/codex')
        )
      ) &&
      github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}
          token: ${{ secrets.REPO_ACCESS_TOKEN }}

      # Give the agent a working environment so it can build/test.
      # Replace with your own setup (install deps, run migrations, etc.).
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci

      - name: Codex autonomous edits
        uses: gersmann/codex-review-action@v1
        with:
          mode: act
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          allowed_commenter_associations: MEMBER,OWNER,COLLABORATOR
```

### `/codex` Commands

- **`/codex <instructions>`** — apply minimal diffs matching the instructions.
- Bare **`/codex`** is ignored; include explicit instructions after the command.
- **`/codex address comments`** (or natural variants like "please fix the review comments") — address unresolved review threads. Only unresolved threads are considered; resolved threads are ignored.

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `openai_api_key` | OpenAI API key | *required* |
| `mode` | `review` or `act` | `review` |
| **Model** | | |
| `model` | Model name | `gpt-5.4` |
| `reasoning_effort` | `minimal` / `low` / `medium` / `high` | `medium` |
| **Review-only** | | |
| `additional_prompt` | Extra reviewer instructions (verbatim) | |
| **Act-only** | | |
| `act_instructions` | Extra guidance appended to the edit prompt | |
| `allowed_commenter_associations` | Comma-separated GitHub `author_association` values allowed to trigger Act mode | `MEMBER,OWNER,COLLABORATOR` |
| `dry_run` | `0` or `1` — skip push | `0` |
| **Debug** | | |
| `debug_level` | `0` (off) / `1` (basic) / `2` (trace) | `1` |
| `stream_agent_messages` | `0` or `1` — stream model output to logs | `1` |
| **Advanced** | | |
| `extra_pip_args` | Additional pip flags (e.g., `--index-url`) | |

## What It Posts

- **Inline comments** anchored to exact diff lines. If a line isn't in the current diff, the finding is skipped.
- **PR-level summary** as an issue comment on each run (refreshed on re-runs; prior summaries are deleted).
- **Multi-line suggestions** only when contiguous and short; otherwise a single-line comment.

## Review Continuation

On repeated `pull_request` review runs, the action now tries to continue the prior Codex review instead of restarting from scratch.

1. The PR summary stores the previously reviewed head SHA in hidden metadata.
2. Review mode caches an isolated Codex home keyed by repository, PR number, model, and reviewed SHA.
3. On the next push, the action restores that cache, resumes the latest stored review thread, and scopes the prompt to the delta since the previously reviewed SHA.
4. If the prior SHA is no longer an ancestor, the cache is missing, or no thread can be restored, the action falls back to a fresh full review.

## Deduplication on Repeated Runs

When a prior Codex review exists on the PR, reruns only reuse **unresolved Codex-authored review threads** as context.

1. **Inline semantic dedup** — prior unresolved Codex comments are passed to the model's structured-output turn so it can avoid reposting the same issue as a new finding.
2. **Re-adjudicated carry-forward** — the model separately marks which of those prior unresolved Codex comments are still relevant now. Only those count toward the PR summary.
3. **Separated counts** — the summary reports new findings from the current run separately from prior Codex findings that still appear relevant.

## Security & Permissions

- Act mode also enforces a built-in `author_association` allowlist. Keep the workflow-level `if:` guard as defense in depth if you want early job skipping.
- Invalid `allowed_commenter_associations` values fail fast at startup so auth policy drift is visible immediately.
- For forks, the default `GITHUB_TOKEN` generally cannot push — run Act only on branches in the main repo, or use a PAT with fork access.
- Grant only what's needed: `contents: write` (push), `pull-requests: write` (reviews), `issues: write` (summary comments and Act replies).

## Troubleshooting

- **422 Unprocessable Entity**: target line not in PR head diff. Rebase and re-run; set `debug_level: 2` to log anchors.
- **Model errors**: ensure your key supports the selected model.
- Review uses built-in prompts (see `prompts/review.md`). Customize with `additional_prompt`.

## Local Development

```bash
uv sync                # install deps
make lint              # format, lint, type-check
GITHUB_TOKEN=… OPENAI_API_KEY=… PYTHONPATH=. python -m cli.main \
  --repo owner/repo --pr 123 --mode review --dry-run
```

Optional: test against a local checkout of `codex-python` instead of PyPI:

```bash
uv sync
uv pip install --editable ../codex-python
```

## Release & Versioning

This repo uses [Release Please](https://github.com/googleapis/release-please) in no-PR mode. Tags and GitHub Releases are created automatically on push to `main`. After publish, the `v1` tag is updated to point to the latest release.

To force a specific version: Actions > "Release Please" > Run workflow > provide `release_as` (e.g., `1.3.0`).
