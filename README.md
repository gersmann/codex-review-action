# Codex Review Action

Run Codex to review pull requests automatically or on demand through `/codex review` comments.

- **Review**: posts precise inline review comments and a PR-level summary. When there are no findings, only the summary is posted.
- **Escalate**: lets trusted reviewers override the model and reasoning effort for a specific PR without granting content write access.

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
        uses: nomadlabsinc/codex-review-action@v1.9.0-nomad.1
        with:
          mode: review
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5.6-terra
          reasoning_effort: low
```

## Review on `/codex review` Comments

When a trusted user comments `/codex review` on a PR, the action runs another review. Optional leading `reasoning:` and `model:` tokens override that run's defaults.

```yaml
name: Codex Comment Review
on:
  issue_comment: { types: [created] }
  pull_request_review_comment: { types: [created] }
permissions:
  contents: read
  pull-requests: write
  issues: write
concurrency:
  group: codex-comment-review-${{ github.event.issue.number || github.event.pull_request.number || github.ref }}
  cancel-in-progress: false
jobs:
  comment-review:
    name: Review on /codex review comments
    if: >-
      (
        (
          github.event_name == 'issue_comment' &&
          startsWith(github.event.comment.body, '/codex review') &&
          github.event.issue.pull_request
        ) || (
          github.event_name == 'pull_request_review_comment' &&
          startsWith(github.event.comment.body, '/codex review')
        )
      ) &&
      github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - name: Verify same-repository PR
        env:
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number || github.event.issue.number }}
        run: |
          head_repository=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" --jq '.head.repo.full_name')
          if [[ "$head_repository" != "$GITHUB_REPOSITORY" ]]; then
            echo "::error::Comment-triggered reviews are limited to same-repository pull requests"
            exit 1
          fi

      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}

      - name: Codex comment-triggered review
        uses: nomadlabsinc/codex-review-action@v1.9.0-nomad.1
        with:
          mode: review
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5.6-terra
          reasoning_effort: low
          web_search_mode: disabled
          allowed_commenter_associations: MEMBER,OWNER,COLLABORATOR
```

### `/codex` Commands

- **`/codex review`** — run a review with the workflow defaults.
- **`/codex review reasoning:xhigh model:gpt-5.6-sol`** — run an escalated review with per-comment overrides.
- Other **`/codex <instructions>`** commands are ignored because this fork is review-only.

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `openai_api_key` | OpenAI API key | *required* |
| `mode` | `review` | `review` |
| **Model** | | |
| `model` | Model name | `gpt-5.4` |
| `reasoning_effort` | `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` (`x-high` is accepted as an alias) | `medium` |
| **Review-only** | | |
| `additional_prompt` | Extra reviewer instructions (verbatim) | |
| `allowed_commenter_associations` | Comma-separated GitHub `author_association` values allowed to trigger comment reviews | `MEMBER,OWNER,COLLABORATOR` |
| `dry_run` | `0` or `1` — do not post comments | `0` |
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
3. **Separated counts** — the summary reports new findings and still-relevant prior findings separately.

## Security & Permissions

- Verify that a comment-triggered PR's head repository matches the base repository before checking out PR code or exposing the OpenAI API key.
- Disable live web search on comment-triggered reviews so untrusted diffs cannot use the review agent as a network exfiltration channel.
- Comment-triggered reviews enforce a built-in `author_association` allowlist. Keep the workflow-level `if:` guard as defense in depth if you want early job skipping.
- Invalid `allowed_commenter_associations` values fail fast at startup so auth policy drift is visible immediately.
- Grant only what's needed: `contents: read`, `pull-requests: write` (reviews), and `issues: write` (summary comments).

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

Nomad fork releases use explicit tags such as `v1.9.0-nomad.1`. Release automation is disabled so no workflow receives `contents: write`; create and push release tags manually after merging verified changes to `main`.
