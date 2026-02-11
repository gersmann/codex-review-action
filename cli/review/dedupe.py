from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from ..models import ExistingReviewComment, FindingLocation
from ..patch_parser import to_relative_path

SUMMARY_MARKER = "Codex Autonomous Review:"


def has_prior_codex_review(
    reviews: Sequence[Any],
    issue_comments: Sequence[Any],
) -> bool:
    for review in reviews:
        review_body = review.body
        if isinstance(review_body, str) and SUMMARY_MARKER in review_body:
            return True

    for issue_comment in issue_comments:
        if isinstance(issue_comment, IssueComment) and SUMMARY_MARKER in (issue_comment.body or ""):
            return True
    return False


def collect_existing_comment_texts(review_comments: Sequence[Any]) -> list[str]:
    """Collect file/diff review comments for semantic dedupe."""
    texts: list[str] = []
    for review_comment in review_comments:
        if not isinstance(review_comment, PullRequestComment):
            continue
        body = review_comment.body.strip()
        path = review_comment.path
        line = review_comment.line or review_comment.original_line
        location = f"{path}:{line}" if path and line else path
        prefix = f"[{location}] " if location else ""
        texts.append(prefix + body)
    return texts


def collect_existing_review_comments(review_comments: Sequence[Any]) -> list[ExistingReviewComment]:
    items: list[ExistingReviewComment] = []
    for review_comment in review_comments:
        if not isinstance(review_comment, PullRequestComment):
            continue

        body = review_comment.body.strip()
        path = review_comment.path
        line = review_comment.line or review_comment.original_line
        if body and path and isinstance(line, int):
            items.append(ExistingReviewComment(path=path, line=int(line), body=body))
    return items


def prefilter_duplicates_by_location(
    findings: list[dict[str, Any]],
    existing: Sequence[ExistingReviewComment],
    rename_map: dict[str, str],
    repo_root: Path,
    *,
    window: int = 3,
) -> list[dict[str, Any]]:
    """Drop findings that are already covered by nearby inline comments."""
    index: dict[str, set[int]] = {}
    for item in existing:
        path = rename_map.get(item.path, item.path)
        if not path:
            continue
        index.setdefault(path, set()).add(item.line)

    if not index:
        return findings

    filtered: list[dict[str, Any]] = []
    for finding in findings:
        location = FindingLocation.from_finding(finding)
        if not location.absolute_file_path or location.start_line <= 0:
            filtered.append(finding)
            continue

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
