from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.config import ReviewConfig
from cli.models import OpenCodexFindingsStats, ReviewRunResult
from cli.review.dedupe import (
    collect_existing_comment_texts_from_threads,
    summarize_open_codex_findings,
)
from cli.workflows.review_workflow import (
    ReviewWorkflow,
    _build_review_summary_with_open_counts,
    _compute_effective_review_result,
)


def _make_workflow() -> ReviewWorkflow:
    return ReviewWorkflow(
        ReviewConfig(github_token="t", repository="o/r"),
        github_client=cast(Any, object()),
        codex_client=cast(Any, object()),
    )


def test_summarize_open_codex_findings_counts_unresolved_priorities() -> None:
    threads = [
        {
            "id": "1",
            "comments": [
                {"path": "a.py", "line": 10, "body": "🔴 [P1] fix guard\n\n**Current code:**"}
            ],
        },
        {
            "id": "2",
            "comments": [{"path": "b.py", "line": 12, "body": "human discussion"}],
        },
        {
            "id": "3",
            "comments": [{"path": "c.py", "line": 3, "body": "⚪ [P3] nit\n\n**Current code:**"}],
        },
    ]

    stats = summarize_open_codex_findings(threads)
    assert stats.total == 2
    assert stats.p0 == 0
    assert stats.p1 == 1
    assert stats.p2 == 0
    assert stats.p3 == 1
    assert stats.blocking == 1
    assert stats.highest_priority == 1


def test_collect_existing_comment_texts_from_threads_only_includes_codex_findings() -> None:
    threads = [
        {"id": "1", "comments": [{"path": "a.py", "line": 2, "body": "🟡 [P2] handle edge case"}]},
        {"id": "2", "comments": [{"path": "a.py", "line": 4, "body": "Looks good to me"}]},
    ]
    texts = collect_existing_comment_texts_from_threads(threads)
    assert texts == ["[a.py:2] 🟡 [P2] handle edge case"]


def test_effective_result_marks_incorrect_for_open_blocking_findings() -> None:
    result = ReviewRunResult(
        overall_correctness="patch is correct",
        overall_explanation="No new issues.",
        findings=[],
    )
    open_findings = OpenCodexFindingsStats(total=1, p1=1)

    effective = _compute_effective_review_result(result, [], open_findings)
    assert effective.overall_correctness == "patch is incorrect"
    assert "blocking findings" in effective.overall_explanation


def test_effective_result_keeps_correct_for_non_blocking_open_findings() -> None:
    result = ReviewRunResult(
        overall_correctness="patch is correct",
        overall_explanation="No new issues.",
        findings=[],
    )
    open_findings = OpenCodexFindingsStats(total=1, p3=1)

    effective = _compute_effective_review_result(result, [], open_findings)
    assert effective.overall_correctness == "patch is correct"


def test_summary_includes_new_and_open_counts() -> None:
    result = ReviewRunResult(
        overall_correctness="patch is incorrect",
        overall_explanation="Outstanding issue remains.",
        findings=[],
    )
    summary = _build_review_summary_with_open_counts(
        result=result,
        new_findings_count=0,
        open_findings=OpenCodexFindingsStats(total=2, p1=1, p2=1),
    )
    assert "- Findings (new): 0" in summary
    assert "- Findings (open): 2" in summary
    assert "- Open blocking findings (P0/P1): 1" in summary


def test_finalize_findings_prefilters_with_unresolved_codex_threads() -> None:
    workflow = _make_workflow()
    finding = {
        "title": "🟡 [P2] duplicate",
        "body": "duplicate body",
        "code_location": {
            "absolute_file_path": str((Path.cwd() / "sample.py").resolve()),
            "line_range": {"start": 10, "end": 10},
        },
    }
    unresolved_threads = [
        {"id": "t1", "comments": [{"path": "sample.py", "line": 10, "body": "🟡 [P2] duplicate"}]}
    ]

    finalized = workflow._finalize_findings(
        findings=[finding],
        rename_map={},
        unresolved_threads=unresolved_threads,
        prior_codex_review=True,
        review_comments_snapshot=[],
    )
    assert finalized == []


def test_finalize_findings_does_not_fallback_when_unresolved_threads_are_empty(
    monkeypatch,
) -> None:
    import cli.workflows.review_workflow as workflow_mod

    workflow = _make_workflow()
    finding = {
        "title": "🟡 [P2] keep me",
        "body": "new finding",
        "code_location": {
            "absolute_file_path": str((Path.cwd() / "sample.py").resolve()),
            "line_range": {"start": 22, "end": 22},
        },
    }

    def _unexpected_collect(review_comments: list[Any]) -> list[Any]:  # noqa: ARG001
        raise AssertionError(
            "should not fallback to full review comments when unresolved threads fetched"
        )

    monkeypatch.setattr(workflow_mod, "collect_existing_review_comments", _unexpected_collect)
    finalized = workflow._finalize_findings(
        findings=[finding],
        rename_map={},
        unresolved_threads=[],
        prior_codex_review=True,
        review_comments_snapshot=[object()],
    )
    assert finalized == [finding]
