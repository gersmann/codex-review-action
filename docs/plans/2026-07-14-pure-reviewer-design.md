# Pure Reviewer Design

## Goal

Make the Action and CLI exclusively responsible for reviewing pull requests. Remove all autonomous-editing behavior and resolve the two open review findings from PR #1.

## Public interface

- Remove the Action `mode` input and the CLI `--mode` flag.
- Remove act-only configuration, environment variables, prompts, workflows, and documentation.
- Keep review-specific controls, including `dry_run`, commenter authorization, and `/codex review` model and reasoning overrides.
- Ignore non-review `/codex` commands with a concise review-only message.
- Default reviews to `gpt-5.6-luna` with `high` reasoning effort.
- Run the bundled reviewer workflow only when an authorized user explicitly comments `/codex review`; do not review automatically when a pull request is opened or updated.

## Runtime flow

The CLI always constructs a review configuration and runs `ReviewWorkflow`. GitHub comment events request reviews through `/codex review`; authorized model or reasoning overrides force a fresh full review. The bundled workflow has no `pull_request` trigger or automatic-review job.

Comment-triggered reviews post results normally. Review state is restored and saved only for `pull_request` events, preventing comment overrides from entering the default review cache.

## Code removal

Delete the edit prompt and edit workflow modules and their dedicated tests. Remove edit-only helpers, models, configuration fields, imports, Action inputs, and environment variables when reference analysis confirms they have no review use. Update package and user documentation so it describes only review behavior.

## Error handling

Invalid review override syntax or values continue to fail clearly. Unauthorized commenters and non-review `/codex` commands remain safe no-ops. Existing high-level exception handling remains responsible for reporting configuration, GitHub, and Codex failures.

## Testing

- Add regression coverage that comment-triggered reviews are not dry runs.
- Add regression coverage that cache saves are restricted to `pull_request` events.
- Strengthen the review-only contract to reject act/edit public surfaces.
- Remove tests that exist only for deleted edit behavior.
- Run the full pytest, Ruff formatting/linting, MyPy, actionlint, and diff checks.
