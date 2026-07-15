from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_workflow_runs_only_requested_reviews() -> None:
    workflow = (ROOT / ".github/workflows/codex-review.yml").read_text(encoding="utf-8")

    triggers = workflow[: workflow.index("concurrency:")]
    assert "  pull_request:\n" not in triggers
    assert "  issue_comment:\n" in triggers
    assert "  pull_request_review_comment:\n" in triggers

    assert "  comment-review:\n" in workflow
    assert "  review:\n" not in workflow
    assert "startsWith(github.event.comment.body, '/codex review')" in workflow
    assert "github.event.issue.pull_request" in workflow
    assert "contents: write" not in workflow
    assert "pull-requests: write" in workflow
    assert "issues: write" in workflow

    assert "refs/pull/{0}/head" in workflow
    assert "mode: review" not in workflow
    assert "model: gpt-5.6-luna" in workflow
    assert "reasoning_effort: high" in workflow
    assert "dry_run: 1" not in workflow


def test_comment_reviews_do_not_restore_or_save_review_cache() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")

    restore_step = action[action.index("    - name: Restore review Codex cache") :]
    restore_condition = restore_step.splitlines()[1]
    assert "github.event_name == 'pull_request'" in restore_condition

    save_step = action[action.index("    - name: Save review Codex cache") :]
    save_condition = save_step.splitlines()[1]
    assert "github.event_name == 'pull_request'" in save_condition


def test_action_defaults_to_luna_with_high_reasoning() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")

    assert 'default: "gpt-5.6-luna"' in action
    assert 'default: "high"' in action
    assert "gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol" in action
