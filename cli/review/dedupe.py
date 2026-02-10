from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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


def deduplicate_findings(
    findings: list[dict[str, Any]],
    existing_comments: list[str],
    *,
    execute_codex: Callable[..., str],
    parse_json_response: Callable[[str], dict[str, Any]],
    fast_model_name: str,
    fast_reasoning_effort: str,
) -> list[dict[str, Any]]:
    """Use the fast model to filter findings already covered by existing comments."""
    compact_findings: list[dict[str, Any]] = []
    for idx, finding in enumerate(findings):
        location = FindingLocation.from_finding(finding)
        compact_findings.append(
            {
                "index": idx,
                "title": str(finding.get("title", "")),
                "body": str(finding.get("body", "")),
                "path": location.absolute_file_path,
                "start": location.start_line,
            }
        )

    instructions = (
        "You are deduplicating review comments.\n"
        'Given `new_findings` and `existing_comments`, return JSON {"keep": [indices]} where indices refer to new_findings[index].\n'
        "Consider a new finding a duplicate if an existing comment already conveys the same issue for the same file and nearby lines,\n"
        "or if it is semantically redundant. Prefer recall (keep) when unsure.\n"
    )
    payload = {
        "new_findings": compact_findings,
        "existing_comments": existing_comments[:200],
    }
    prompt = (
        instructions
        + "\n\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nOUTPUT: JSON with only the 'keep' array."
    )

    try:
        raw = execute_codex(
            prompt,
            model_name=fast_model_name,
            reasoning_effort=fast_reasoning_effort,
            suppress_stream=True,
            config_overrides={"sandbox_mode": "danger-full-access"},
        )
        data = parse_json_response(raw)
        keep_raw = data.get("keep")
        if not isinstance(keep_raw, list):
            return findings
        keep_set = {
            int(idx)
            for idx in keep_raw
            if isinstance(idx, int) or (isinstance(idx, str) and str(idx).isdigit())
        }
        return [finding for i, finding in enumerate(findings) if i in keep_set]
    except Exception:
        return findings
