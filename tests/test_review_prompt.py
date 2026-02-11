from __future__ import annotations

from typing import Any, cast

from cli.config import ReviewConfig
from cli.review_prompt import PromptBuilder
from cli.workflows.review_workflow import ReviewWorkflow


def _make_review_config() -> ReviewConfig:
    return ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="review",
    )


def test_load_guidelines_include_repo_standard_comment_format() -> None:
    builder = PromptBuilder(_make_review_config())
    guidelines = builder.load_guidelines()

    assert "REVIEW COMMENT FORMAT (REPO STANDARD):" in guidelines
    assert "**Current code:**" in guidelines
    assert "**Problem:** Brief description (max 20 words)." in guidelines
    assert "**Fix:**" in guidelines
    assert "severity emoji + priority tag: 🔴 [P0]/[P1], 🟡 [P2], ⚪ [P3]" in guidelines
    assert "Include file path and line number in the title when possible." in guidelines
    assert "Skip comments for formatting-only issues, personal style preferences" in guidelines

    # Preserve required JSON output fields.
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
