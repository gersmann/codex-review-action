from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..core.github_types import (
    IssueCommentLikeProtocol,
    ReviewCommentLikeProtocol,
    ReviewLikeProtocol,
)
from ..core.models import (
    ExistingReviewComment,
    FindingLocation,
    ReviewCommentSnapshot,
    ReviewFinding,
)
from .patch_parser import to_relative_path

SUMMARY_MARKER = "Codex Autonomous Review:"


def has_prior_codex_review(
    reviews: Sequence[ReviewLikeProtocol],
    issue_comments: Sequence[IssueCommentLikeProtocol],
) -> bool:
    for review in reviews:
        review_body = review.body
        if isinstance(review_body, str) and SUMMARY_MARKER in review_body:
            return True

    for issue_comment in issue_comments:
        body = issue_comment.body
        if isinstance(body, str) and SUMMARY_MARKER in body:
            return True
    return False


def collect_existing_comment_texts(
    review_comments: Sequence[ReviewCommentLikeProtocol],
) -> list[str]:
    """Collect file/diff review comments for semantic dedupe."""
    texts: list[str] = []
    for review_comment in review_comments:
        comment = _normalize_review_comment(review_comment)
        body, path, line = comment
        location = f"{path}:{line}" if path and line else path
        prefix = f"[{location}] " if location else ""
        texts.append(prefix + body)
    return texts


def collect_existing_review_comments(
    review_comments: Sequence[ReviewCommentLikeProtocol],
) -> list[ExistingReviewComment]:
    items: list[ExistingReviewComment] = []
    for review_comment in review_comments:
        comment = _normalize_review_comment(review_comment)
        body, path, line = comment
        if body and path and isinstance(line, int):
            items.append(ExistingReviewComment(path=path, line=int(line), body=body))
    return items


def prefilter_duplicates_by_location(
    findings: list[ReviewFinding],
    existing: Sequence[ExistingReviewComment],
    rename_map: dict[str, str],
    repo_root: Path,
    *,
    window: int = 3,
) -> list[ReviewFinding]:
    """Drop findings that are already covered by nearby inline comments."""
    index: dict[str, set[int]] = {}
    for item in existing:
        path = rename_map.get(item.path, item.path)
        if not path:
            continue
        index.setdefault(path, set()).add(item.line)

    if not index:
        return findings

    filtered: list[ReviewFinding] = []
    for finding in findings:
        location = FindingLocation.from_review_finding(finding)

        rel_path = to_relative_path(location.absolute_file_path, repo_root)
        rel_path = rename_map.get(rel_path, rel_path)
        lines = index.get(rel_path)
        if not lines:
            filtered.append(finding)
            continue

        is_duplicate = any(
            abs(line - location.start_line) <= window or abs(line - location.end_line) <= window
            for line in lines
            if line > 0
        )
        if not is_duplicate:
            filtered.append(finding)

    return filtered


def _normalize_review_comment(
    review_comment: ReviewCommentLikeProtocol,
) -> tuple[str, str, int | None]:
    snapshot = ReviewCommentSnapshot.from_review_comment(review_comment)
    return snapshot.body, snapshot.path, snapshot.prompt_line
