from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.config import ReviewConfig
from cli.models import (
    ExistingReviewComment,
    OpenCodexFindingsStats,
    PriorCodexFinding,
    ReviewRunResult,
)
from cli.review.dedupe import (
    collect_existing_comment_texts_from_prior_findings,
    extract_prior_codex_findings,
    summarize_prior_codex_findings,
)
from cli.workflows.review_workflow import (
    ReviewWorkflow,
    _build_review_summary_with_open_counts,
    _canonical_overall_correctness,
    _compute_effective_review_result,
)


def _make_workflow() -> ReviewWorkflow:
    return ReviewWorkflow(
        ReviewConfig(github_token="t", repository="o/r"),
        github_client=cast(Any, object()),
        codex_client=cast(Any, object()),
    )


def test_extract_prior_codex_findings_counts_priorities() -> None:
    threads = [
        {
            "id": "1",
            "is_resolved": False,
            "comments": [
                {"path": "a.py", "line": 10, "body": "🔴 [P1] fix guard\n\n**Current code:**"}
            ],
        },
        {
            "id": "2",
            "is_resolved": True,
            "comments": [{"path": "b.py", "line": 12, "body": "human discussion"}],
        },
        {
            "id": "3",
            "is_resolved": True,
            "comments": [{"path": "c.py", "line": 3, "body": "⚪ [P3] nit\n\n**Current code:**"}],
        },
    ]

    prior = extract_prior_codex_findings(threads)
    stats = summarize_prior_codex_findings(prior)
    assert [item.thread_id for item in prior] == ["1", "3"]
    assert stats.total == 2
    assert stats.p0 == 0
    assert stats.p1 == 1
    assert stats.p2 == 0
    assert stats.p3 == 1
    assert stats.blocking == 1
    assert stats.highest_priority == 1


def test_collect_existing_comment_texts_from_prior_findings() -> None:
    findings = [
        PriorCodexFinding(
            id="t1:c1",
            thread_id="t1",
            comment_id="c1",
            title="🟡 [P2] handle edge case",
            body="🟡 [P2] handle edge case",
            path="a.py",
            line=2,
            priority=2,
            is_resolved=False,
        )
    ]
    texts = collect_existing_comment_texts_from_prior_findings(findings)
    assert texts == ["[a.py:2] 🟡 [P2] handle edge case"]


def test_extract_prior_codex_findings_prefers_latest_codex_comment_in_thread() -> None:
    threads = [
        {
            "id": "1",
            "is_resolved": False,
            "comments": [
                {"id": "old", "path": "a.py", "line": 10, "body": "🟡 [P2] older priority"},
                {"id": "new", "path": "a.py", "line": 10, "body": "🔴 [P1] newer priority"},
            ],
        }
    ]
    prior = extract_prior_codex_findings(threads)
    stats = summarize_prior_codex_findings(prior)
    assert prior and prior[0].comment_id == "new"
    assert stats.total == 1
    assert stats.p1 == 1
    assert stats.p2 == 0


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


def test_effective_result_keeps_model_verdict_when_prior_applicability_unknown() -> None:
    result = ReviewRunResult(
        overall_correctness="patch is correct",
        overall_explanation="No new issues.",
        findings=[],
    )
    unknown_prior = OpenCodexFindingsStats.unknown_stats()
    effective = _compute_effective_review_result(result, [], unknown_prior)
    assert effective.overall_correctness == "patch is correct"
    assert "unavailable" in effective.overall_explanation


def test_canonical_overall_correctness_handles_trailing_period() -> None:
    assert _canonical_overall_correctness("patch is incorrect.") == "patch is incorrect"


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
    assert "- Findings (applicable prior): 2" in summary
    assert "- Applicable prior blocking findings (P0/P1): 1" in summary


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
    existing = [ExistingReviewComment(path="sample.py", line=10, body="🟡 [P2] duplicate")]

    finalized = workflow._finalize_findings(
        findings=[finding],
        rename_map={},
        existing_struct=existing,
    )
    assert finalized == []


def test_finalize_findings_keeps_findings_without_existing_struct() -> None:
    workflow = _make_workflow()
    finding = {
        "title": "🟡 [P2] keep me",
        "body": "new finding",
        "code_location": {
            "absolute_file_path": str((Path.cwd() / "sample.py").resolve()),
            "line_range": {"start": 22, "end": 22},
        },
    }
    finalized = workflow._finalize_findings(
        findings=[finding],
        rename_map={},
        existing_struct=[],
    )
    assert finalized == [finding]
