# Pure Reviewer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove every autonomous-editing surface and make the Action and CLI exclusively perform explicitly requested pull-request reviews while fixing PR #1's two open findings.

**Architecture:** Keep `ReviewWorkflow` as the only workflow entry point. The bundled workflow runs only for authorized `/codex review` comments, defaults to `gpt-5.6-luna` with high reasoning, and never runs automatically on pull-request submission. Comment reviews do not restore or save persistent review caches. Delete edit-only modules and then prune support code by reference rather than retaining compatibility shims.

**Tech Stack:** Python 3.12, pytest, Ruff, MyPy, composite GitHub Actions YAML, actionlint.

---

### Task 1: Lock in the Action regressions

**Files:**
- Modify: `tests/test_review_only_contract.py`
- Modify: `.github/workflows/codex-review.yml`
- Modify: `action.yml`

**Step 1: Write the failing tests**

Extend the review-only contract with direct assertions for both PR #1 findings and the removed mode input:

```python
assert "  mode:" not in action_text
assert "inputs.mode" not in action_text
assert "CODEX_MODE" not in action_text
assert "github.event_name == 'pull_request'" in cache_save_condition
assert "dry_run: 1" not in comment_job
assert "pull_request:" not in workflow_triggers
assert "model: gpt-5.6-luna" in comment_job
assert "reasoning_effort: high" in comment_job
```

Extract `comment_job` and `cache_save_condition` from the relevant YAML sections so each assertion is scoped to the behavior under test.

**Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/test_review_only_contract.py -q`

Expected: failures for the existing `mode` input, `inputs.mode` conditions, missing cache event guard, and comment-review `dry_run: 1`.

**Step 3: Implement the minimal Action changes**

- Remove the `mode` input and validation from `action.yml`.
- Remove review-mode conditionals and `CODEX_MODE`; the Action always prepares, restores, runs, and saves a review.
- Add `github.event_name == 'pull_request'` to both review-cache restore and save conditions so comment-triggered overrides cannot use persistent default caches.
- Remove `dry_run: 1` from the comment-review job.
- Remove redundant `mode: review` values from the self-review workflow.
- Remove the `pull_request` trigger and automatic-review job from the self-review workflow.
- Change Action/CLI and self-review defaults to `gpt-5.6-luna` with `high` reasoning effort.

**Step 4: Run the tests and verify GREEN**

Run: `uv run pytest tests/test_review_only_contract.py -q`

Expected: all contract tests pass.

**Step 5: Commit**

```bash
git add action.yml .github/workflows/codex-review.yml tests/test_review_only_contract.py
git commit -m "fix: post comment reviews without caching overrides"
```

### Task 2: Make the CLI and configuration review-only

**Files:**
- Modify: `tests/test_main.py`
- Modify: `tests/test_config.py`
- Modify: `cli/main.py`
- Modify: `cli/core/config.py`

**Step 1: Write the failing tests**

Add assertions that the parser has no `mode` or `act_instructions` destinations, environment configuration ignores obsolete `CODEX_MODE` and `CODEX_ACT_INSTRUCTIONS`, and the workflow dispatcher always instantiates `ReviewWorkflow`:

```python
parser_dests = {action.dest for action in create_parser()._actions}
assert "mode" not in parser_dests
assert "act_instructions" not in parser_dests
assert not hasattr(config, "mode")
assert not hasattr(config, "act_instructions")
```

Update existing review/comment tests to construct review-only configurations and remove mocks or expectations for `EditWorkflow`.

**Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/test_main.py tests/test_config.py -q`

Expected: failures showing the parser, config dataclass, environment loader, and dispatcher still expose act mode.

**Step 3: Implement the minimal CLI/config changes**

- Remove the `EditWorkflow` import, `--mode`, and `--act-instructions`.
- Rename `extract_edit_command` to a review-neutral command parser.
- Remove the mode branch from the dispatcher and run `ReviewWorkflow` directly.
- Remove `mode` and `act_instructions` from `ReviewConfig`, `_ReviewConfigValues`, override keys, validation, environment loading, and override application.
- Change review-only error messages and docstrings so none refer to edit mode.

**Step 4: Run the tests and verify GREEN**

Run: `uv run pytest tests/test_main.py tests/test_config.py -q`

Expected: all CLI and configuration tests pass.

**Step 5: Commit**

```bash
git add cli/main.py cli/core/config.py tests/test_main.py tests/test_config.py
git commit -m "refactor: make cli review-only"
```

### Task 3: Delete autonomous-editing implementation and support code

**Files:**
- Delete: `cli/workflows/edit_prompt.py`
- Delete: `cli/workflows/edit_workflow.py`
- Delete: `tests/test_address_comments.py`
- Delete: `tests/test_edit_prompt.py`
- Modify: `tests/test_review_only_contract.py`
- Modify: `cli/clients/git_ops.py`
- Modify: `cli/clients/github_client.py`
- Modify: `cli/core/github_types.py`
- Modify: `cli/core/models.py`
- Modify: `cli/workflows/review_workflow.py`
- Modify: `tests/test_git_ops.py`
- Modify: `tests/test_module_coverage.py`

**Step 1: Write the failing contract test**

Assert that edit modules are absent and production source contains no edit-only identifiers:

```python
assert not (ROOT / "cli/workflows/edit_workflow.py").exists()
assert not (ROOT / "cli/workflows/edit_prompt.py").exists()
for forbidden in ("EditWorkflow", "act_instructions", "address comments"):
    assert forbidden not in production_text
```

**Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_review_only_contract.py -q`

Expected: failure because edit modules and edit-only identifiers still exist.

**Step 3: Delete edit code and prune support code**

- Delete the edit workflow/prompt modules and their dedicated tests.
- Remove edit-only Git operations, snapshots, push/rebase helpers, and their tests; retain only helpers used by review resume/diff logic.
- Remove `CommentContext`, unresolved-edit models, edit-only GitHub client methods, and `get_review_comment` protocol support when reference searches confirm they are unused by review flow.
- Remove edit-only sections from the mixed `test_module_coverage.py` suite.
- Replace the review summary tip that advertises `/codex address comments` with `/codex review` guidance.

**Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_review_only_contract.py tests/test_git_ops.py tests/test_module_coverage.py tests/test_review_workflow.py -q`

Expected: all focused tests pass.

**Step 5: Commit**

```bash
git add -A cli tests
git commit -m "refactor: remove autonomous editing code"
```

### Task 4: Remove edit-mode documentation and metadata

**Files:**
- Modify: `README.md`
- Modify: `cli/README.md`
- Modify: `pyproject.toml`
- Modify: `AGENTS.md`
- Modify: `tests/test_review_only_contract.py`

**Step 1: Extend the failing repository contract**

Scan user-facing documentation and package metadata for obsolete public concepts:

```python
for path in (ROOT / "README.md", ROOT / "cli/README.md", ROOT / "pyproject.toml"):
    text = path.read_text(encoding="utf-8").lower()
    assert "--mode" not in text
    assert "mode act" not in text
    assert "autonomous edit" not in text
```

**Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_review_only_contract.py -q`

Expected: documentation and package metadata still describe edits or mode selection.

**Step 3: Rewrite documentation for the pure reviewer**

- Remove act-mode architecture, commands, inputs, environment variables, and examples.
- Keep `/codex review` requests/overrides, review dry runs, security, and release guidance; remove automatic-review instructions.
- Update `pyproject.toml` and repository guidelines to describe only reviews.

**Step 4: Run contract tests and repository-wide searches**

Run: `uv run pytest tests/test_review_only_contract.py -q`

Run: `rg -n "edit_workflow|edit_prompt|act_instructions|CODEX_MODE|CODEX_ACT_INSTRUCTIONS|--mode|mode: act|autonomous edits?" README.md cli action.yml .github tests pyproject.toml AGENTS.md`

Expected: tests pass and the search returns no obsolete feature references.

**Step 5: Commit**

```bash
git add README.md cli/README.md pyproject.toml AGENTS.md tests/test_review_only_contract.py
git commit -m "docs: describe pure review workflow"
```

### Task 5: Full verification and cleanup

**Files:**
- Modify only files required by verification failures.

**Step 1: Run the complete test suite**

Run: `uv run pytest -q`

Expected: all tests pass.

**Step 2: Run formatting, linting, and typing**

Run: `uv run ruff format --check .`

Run: `uv run ruff check .`

Run: `uv run mypy .`

Expected: all commands pass without changes or diagnostics.

**Step 3: Validate workflow YAML**

Run: `actionlint .github/workflows/*.yml`

Expected: no diagnostics.

**Step 4: Inspect the final diff**

Run: `git diff main...HEAD --check`

Run: `git status --short`

Run: `git diff --stat main...HEAD`

Expected: no whitespace errors, a clean worktree, and a deletion-heavy review-only diff.

**Step 5: Commit verification fixes if needed**

```bash
git add -A
git commit -m "chore: finish pure reviewer cleanup"
```
