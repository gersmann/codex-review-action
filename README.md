# Codex Review Action

Run focused pull-request reviews when an authorized reviewer comments `/review`, and adjudicate individual reviewer claims when someone comments `/verify`. The bundled workflow does not run when a pull request is opened or updated.

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
        github.event.comment.body == '/review' ||
        github.event.comment.body == '/verify' ||
        startsWith(github.event.comment.body, '/review ') ||
        startsWith(github.event.comment.body, '/review:') ||
        startsWith(github.event.comment.body, '/verify ') ||
        startsWith(github.event.comment.body, '/verify:') ||
        startsWith(github.event.comment.body, '/codex ') ||
        startsWith(github.event.comment.body, '/codex:')
      ) &&
      (
        github.event_name == 'pull_request_review_comment' ||
        github.event.issue.pull_request
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
          allowed_commenter_associations: MEMBER,OWNER,COLLABORATOR
```

## Commands

- `/review` runs a review with `gpt-5.6-luna` and xhigh reasoning by default.
- When reasoning is not specified, the default follows the model: `xhigh` for
  `gpt-5.6-luna`, `medium` for `gpt-5.6-terra` and `gpt-5.6-sol`.
- `/review reasoning:xhigh model:gpt-5.6-sol` overrides one requested review.
- The `gpt-5.6-` prefix is optional: `luna`, `sol`, and `terra` work as
  shorthands. `/review sol` picks the model (reasoning defaults apply), and
  `/review luna:high` sets both the model and its reasoning effort.
  `model:luna` expands the same way.
- `/verify` adjudicates a reviewer claim; see
  [Verifying a reviewer comment](#verifying-a-reviewer-comment).
- Other commands are ignored.

After authorizing a request, the reviewer immediately adds a rocket reaction
and replies that the request is queued. Inline requests receive inline replies.
The queued reply is deleted once the review summary or verify verdict posts;
the rocket reaction stays. If the run fails, the queued reply remains.

### Verifying a reviewer comment

Reply to any inline review comment with `/verify` and Codex adjudicates
whether that thread's claim is correct, replying in the same thread with a
verdict (`correct` / `incorrect` / `uncertain`), an evidence-based
explanation, a confidence score, time elapsed, and usage stats.

- As a top-level PR comment, state the claim yourself:
  `/verify does the retry loop drop the last attempt?`
- Options match `/review` (including model shorthands) and must come before
  the claim text: `/verify terra:xhigh <claim>`
- Defaults are the same as review: `gpt-5.6-luna`, with reasoning defaulting
  to `xhigh` on `gpt-5.6-luna` and `medium` on `gpt-5.6-terra`/`gpt-5.6-sol`
  when not specified.
- As with reviews, the queued acknowledgement is deleted once the verdict
  posts.

## Inputs

| Input | Description | Default |
|---|---|---|
| `openai_api_key` | OpenAI API key | Required |
| `model` | `gpt-5.6-luna`, `gpt-5.6-terra`, or `gpt-5.6-sol` | `gpt-5.6-luna` |
| `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max` | Empty: `xhigh` for `gpt-5.6-luna`, `medium` for `gpt-5.6-terra`/`gpt-5.6-sol` |
| `web_search_mode` | `disabled`, `cached`, or `live` | `disabled` |
| `additional_prompt` | Extra reviewer instructions | Empty |
| `allowed_commenter_associations` | GitHub roles allowed to request reviews and verifications | `MEMBER,OWNER,COLLABORATOR` |
| `dry_run` | Print payloads without posting when set to `1` | `0` |
| `debug_level` | Debug verbosity from `0` to `2` | `1` |
| `stream_agent_messages` | Stream model output when set to `1` | `1` |

## Review Output

- A rocket reaction and short queued reply on the review request; the queued
  reply is removed after the summary posts.
- Inline comments anchored to current diff lines.
- A refreshed PR summary with new findings, still-relevant prior findings,
  observed model responses, token usage, estimated cost, and time elapsed
  (for example `- Time elapsed: 4m 32s`).
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

- The bundled workflow has no `pull_request` trigger and requires an explicit `/review` or `/verify` comment.
- It verifies that the PR head belongs to the same repository before checkout or API-key exposure.
- It disables live web search for comment-triggered runs.
- It grants `contents: read`, `pull-requests: write`, and `issues: write`; it never receives content write access.
- The CLI validates the commenter association before starting a requested review or verification.

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
