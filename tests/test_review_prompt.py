from __future__ import annotations

from typing import Any, cast

from cli.core.config import ReviewConfig
from cli.review.review_prompt import load_guidelines
from cli.workflows.review_workflow import ReviewWorkflow


def _make_review_config() -> ReviewConfig:
    return ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="review",
    )


def test_load_guidelines_include_repo_standard_comment_format() -> None:
    guidelines = load_guidelines(_make_review_config())

    assert "REVIEW COMMENT FORMAT (REPO STANDARD):" in guidelines
    assert "**Current code:**" in guidelines
    assert "**Problem:** Brief description (max 20 words)." in guidelines
    assert "**Fix:**" in guidelines
    assert "severity emoji + priority tag: 🔴 [P0]/[P1], 🟡 [P2], ⚪ [P3]" in guidelines
    assert "Include file path and line number in the title when possible." in guidelines
    assert "Skip comments for formatting-only issues, personal style preferences" in guidelines

    # Preserve required JSON output fields.
    assert '"carried_forward": [' in guidelines
    assert '"comment_id": "<prior review comment id>"' in guidelines
    assert '"current_evidence": "<exact current-code snippet copied verbatim>"' in guidelines
    assert '"resolved_comment_ids": [' in guidelines
    assert '"overall_correctness": "patch is correct" | "patch is incorrect"' in guidelines
    assert '"code_location": {' in guidelines


def test_review_base_instructions_mark_repo_standard_as_authoritative() -> None:
    workflow = ReviewWorkflow(
        _make_review_config(),
        github_client=cast(Any, object()),
        codex_client=cast(Any, object()),
    )

    instructions = workflow._build_review_base_instructions("dummy")

    assert "REVIEW COMMENT FORMAT (REPO STANDARD)" in instructions
    assert "authoritative" in instructions


def test_review_base_instructions_do_not_duplicate_additional_prompt() -> None:
    config = _make_review_config()
    config.additional_prompt = "Custom instruction"
    workflow = ReviewWorkflow(
        config,
        github_client=cast(Any, object()),
        codex_client=cast(Any, object()),
    )

    instructions = workflow._build_review_base_instructions("dummy")

    assert "Custom instruction" not in instructions
