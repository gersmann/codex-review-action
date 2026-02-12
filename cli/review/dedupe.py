from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from ..models import ExistingReviewComment, FindingLocation, OpenCodexFindingsStats
from ..patch_parser import to_relative_path

SUMMARY_MARKER = "Codex Autonomous Review:"
_PRIORITY_TAG_RE = re.compile(r"\[P([0-3])\]")


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


def collect_existing_comment_texts_from_threads(threads: Sequence[dict[str, Any]]) -> list[str]:
    """Collect unresolved Codex finding comments from review threads for semantic dedupe."""
    texts: list[str] = []
    for thread in threads:
        finding_comment = _extract_codex_finding_comment(thread)
        if finding_comment is None:
            continue

        location = finding_comment.path
        if location and finding_comment.line > 0:
            location = f"{location}:{finding_comment.line}"
        prefix = f"[{location}] " if location else ""
        texts.append(prefix + finding_comment.body)
    return texts


def collect_existing_review_comments_from_threads(
    threads: Sequence[dict[str, Any]],
) -> list[ExistingReviewComment]:
    """Collect unresolved Codex finding comments from review threads for location dedupe."""
    items: list[ExistingReviewComment] = []
    for thread in threads:
        finding_comment = _extract_codex_finding_comment(thread)
        if finding_comment is None:
            continue
        if not finding_comment.path or finding_comment.line <= 0:
            continue
        items.append(
            ExistingReviewComment(
                path=finding_comment.path,
                line=finding_comment.line,
                body=finding_comment.body,
            )
        )
    return items


def summarize_open_codex_findings(threads: Sequence[dict[str, Any]]) -> OpenCodexFindingsStats:
    """Summarize unresolved Codex findings from unresolved review threads."""
    counts = [0, 0, 0, 0]
    for thread in threads:
        finding_comment = _extract_codex_finding_comment(thread)
        if finding_comment is None:
            continue
        counts[finding_comment.priority] += 1

    return OpenCodexFindingsStats(
        total=sum(counts),
        p0=counts[0],
        p1=counts[1],
        p2=counts[2],
        p3=counts[3],
    )


def parse_priority_tag(text: str) -> int | None:
    """Extract normalized priority [P0-P3] from text."""
    if not text:
        return None
    match = _PRIORITY_TAG_RE.search(text)
    if not match:
        return None
    try:
        parsed = int(match.group(1))
    except ValueError:
        return None
    if 0 <= parsed <= 3:
        return parsed
    return None


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


class _ThreadFindingComment:
    def __init__(self, *, body: str, path: str, line: int, priority: int) -> None:
        self.body = body
        self.path = path
        self.line = line
        self.priority = priority


def _extract_codex_finding_comment(thread: dict[str, Any]) -> _ThreadFindingComment | None:
    comments_value = thread.get("comments")
    if not isinstance(comments_value, list):
        return None

    for comment in comments_value:
        if not isinstance(comment, dict):
            continue

        body_value = comment.get("body")
        if not isinstance(body_value, str):
            continue
        body = body_value.strip()
        if not body:
            continue

        first_line = body.splitlines()[0].strip()
        priority = parse_priority_tag(first_line)
        if priority is None:
            continue

        path_value = comment.get("path")
        path = path_value if isinstance(path_value, str) else ""

        line = _as_positive_int(comment.get("line"))
        if line <= 0:
            line = _as_positive_int(comment.get("original_line"))

        return _ThreadFindingComment(body=body, path=path, line=line, priority=priority)

    return None


def _as_positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
