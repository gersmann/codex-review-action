from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_exposes_review_mode_only() -> None:
    action_text = (ROOT / "action.yml").read_text(encoding="utf-8")

    assert action_text.startswith('name: "Codex Code Review"\n')
    assert 'description: "Operation mode: review"' in action_text
    assert "review|act" not in action_text
    assert "CODEX_ACT_INSTRUCTIONS" not in action_text
    assert (
        'PYTHONPATH="${{ github.action_path }}:${PYTHONPATH:-}" '
        "python3 -m cli.core.reasoning_effort"
    ) in action_text


def test_self_review_workflow_routes_review_comments_without_write_access() -> None:
    workflow_text = (ROOT / ".github/workflows/codex-review.yml").read_text(encoding="utf-8")

    assert "contents: write" not in workflow_text
    assert "comment-review:" in workflow_text
    assert "startsWith(github.event.comment.body, '/codex review')" in workflow_text
    assert "mode: act" not in workflow_text

    comment_job_index = workflow_text.index("  comment-review:")
    guard_index = workflow_text.index("Verify same-repository PR", comment_job_index)
    checkout_index = workflow_text.index("actions/checkout@v4", guard_index)
    secret_index = workflow_text.index("openai_api_key:", guard_index)

    assert comment_job_index < guard_index < checkout_index < secret_index
    assert 'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"' in workflow_text
    assert '"$head_repository" != "$GITHUB_REPOSITORY"' in workflow_text
    assert "web_search_mode: disabled" in workflow_text


def test_no_workflow_has_contents_write_permission() -> None:
    workflow_dir = ROOT / ".github/workflows"

    for workflow_path in workflow_dir.glob("*.yml"):
        workflow_text = workflow_path.read_text(encoding="utf-8")
        assert "contents: write" not in workflow_text, workflow_path.name
