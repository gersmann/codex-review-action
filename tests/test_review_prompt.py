from __future__ import annotations

from typing import Any, cast

from cli.core.config import ReviewConfig
from cli.review.review_prompt import load_guidelines
from cli.workflows.review_workflow import ReviewWorkflow


def _make_review_config() -> ReviewConfig:
    return ReviewConfig(
        github_token="token",
        repository="owner/repo",
    )


def test_load_guidelines_match_upstream_codex_rubric() -> None:
    guidelines = load_guidelines(_make_review_config())

    # Upstream rubric content, vendored verbatim.
    assert "You are acting as a reviewer for a proposed code change" in guidelines
    assert "The body should be at most 1 paragraph." in guidelines
    assert "should not include any chunks of code longer than 3 lines" in guidelines
    assert '"[P1] Un-padding slices along wrong tensor dimensions"' in guidelines

    # Retired repo-specific formatting must stay gone.
    assert "REVIEW COMMENT FORMAT (REPO STANDARD)" not in guidelines
    assert "**Current code:**" not in guidelines
    assert "severity emoji" not in guidelines
    assert "🔴" not in guidelines


def test_load_guidelines_include_integration_addendum() -> None:
    guidelines = load_guidelines(_make_review_config())

    assert "# Integration addendum (codex-review-action)" in guidelines

    # Preserve required JSON output fields.
    assert '"carried_forward": [' in guidelines
    assert '"comment_id": "<prior review comment id>"' in guidelines
    assert '"current_evidence": "<exact current-code snippet copied verbatim>"' in guidelines
    assert '"overall_correctness": "patch is correct" | "patch is incorrect"' in guidelines
    assert '"code_location": {' in guidelines

    # Knowledge-cutoff guard for the correctness verdict.
    assert "UNVERIFIABLE FACTS:" in guidelines
    assert "may have changed after your knowledge cutoff" in guidelines


def test_load_verify_guidelines() -> None:
    from cli.review.review_prompt import load_verify_guidelines

    guidelines = load_verify_guidelines(_make_review_config())

    assert "adjudicating" in guidelines
    assert '"verdict": "correct" | "incorrect" | "uncertain"' in guidelines
    assert "may have changed after your knowledge cutoff" in guidelines


def test_review_base_instructions_have_no_repo_standard_reference() -> None:
    workflow = ReviewWorkflow(
        _make_review_config(),
        github_client=cast(Any, object()),
        codex_client=cast(Any, object()),
    )

    instructions = workflow._build_review_base_instructions("dummy")

    assert "REVIEW COMMENT FORMAT (REPO STANDARD)" not in instructions
    assert "Review guidelines:\ndummy" in instructions


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
