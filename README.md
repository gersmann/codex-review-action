# Codex Review Action

Run focused pull-request reviews when an authorized reviewer comments `/review`. The bundled workflow does not run when a pull request is opened or updated.

The reviewer posts precise inline findings and refreshes a PR-level summary. It never edits repository contents.

## On-Demand Workflow

```yaml
name: Codex On-Demand Review
on:
  issue_comment: { types: [created] }
  pull_request_review_comment: { types: [created] }
permissions:
  contents: read
  pull-requests: write
  issues: write
concurrency:
  group: codex-review-${{ github.event.issue.number || github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
jobs:
  comment-review:
    if: >-
      (
        (
          github.event_name == 'issue_comment' &&
          (
            startsWith(github.event.comment.body, '/review') ||
            startsWith(github.event.comment.body, '/verify')
          ) &&
          github.event.issue.pull_request
        ) || (
          github.event_name == 'pull_request_review_comment' &&
          (
            startsWith(github.event.comment.body, '/review') ||
            startsWith(github.event.comment.body, '/verify')
          )
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

      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}

      - name: Codex review
        uses: nomadlabsinc/codex-review-action@v1.9.0-nomad.1
        with:
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          model: gpt-5.6-luna
          reasoning_effort: high
          web_search_mode: disabled
          allowed_commenter_associations: MEMBER,OWNER,COLLABORATOR
```

## Commands

- `/review` runs a review with `gpt-5.6-luna` and high reasoning by default.
- `/review reasoning:xhigh model:gpt-5.6-sol` overrides one requested review.
- Other commands are ignored.

After authorizing a request, the reviewer immediately adds a rocket reaction
and replies that the review is queued. Inline requests receive inline replies.

## Inputs

| Input | Description | Default |
|---|---|---|
| `openai_api_key` | OpenAI API key | Required |
| `model` | `gpt-5.6-luna`, `gpt-5.6-terra`, or `gpt-5.6-sol` | `gpt-5.6-luna` |
| `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max` | `high` |
| `web_search_mode` | `disabled`, `cached`, or `live` | `live` |
| `additional_prompt` | Extra reviewer instructions | Empty |
| `allowed_commenter_associations` | GitHub roles allowed to request reviews | `MEMBER,OWNER,COLLABORATOR` |
| `dry_run` | Print payloads without posting when set to `1` | `0` |
| `debug_level` | Debug verbosity from `0` to `2` | `1` |
| `stream_agent_messages` | Stream model output when set to `1` | `1` |
| `extra_pip_args` | Additional pip installation flags | Empty |

## Review Output

- A rocket reaction and short queued reply on the review request.
- Inline comments anchored to current diff lines.
- A refreshed PR summary with new findings, still-relevant prior findings,
  observed model responses, token usage, and estimated cost.
- No comment when an inline finding cannot be anchored safely; the summary reports dropped findings.

Prior unresolved Codex-authored threads are supplied as context so repeated requested reviews avoid duplicating findings and can identify which earlier findings remain relevant.

## Usage and Cost Estimates

The summary prices observed token usage with these static per-million-token
rates:

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.6-luna` | $1.00 | $0.10 | $6.00 |
| `gpt-5.6-terra` | $2.50 | $0.25 | $15.00 |
| `gpt-5.6-sol` | $5.00 | $0.50 | $30.00 |

Cached input is included in the input total and priced at its lower rate.
Reasoning output is included in output tokens and is not charged twice. The
amount remains an estimate because provider pricing can change and the protocol
does not expose every pricing dimension.

## Security

- The bundled workflow has no `pull_request` trigger and requires an explicit `/review` comment.
- It verifies that the PR head belongs to the same repository before checkout or API-key exposure.
- It disables live web search for comment-triggered reviews.
- It grants `contents: read`, `pull-requests: write`, and `issues: write`; it never receives content write access.
- The CLI validates the commenter association before starting a requested review.

## Local Development

```bash
uv sync
uv run pytest -q
make qa
GITHUB_TOKEN=… OPENAI_API_KEY=… PYTHONPATH=. python -m cli.main \
  --repo owner/repo --pr 123 --dry-run
```

Review guidelines live in `prompts/review.md`. Use `additional_prompt` or `CODEX_ADDITIONAL_PROMPT` for repository-specific guidance.

## Release and Versioning

Nomad fork releases use explicit tags such as `v1.9.0-nomad.1`. Release automation is disabled so no workflow receives content write access.
