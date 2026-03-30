from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..core.github_types import IssueCommentLikeProtocol, ReviewLikeProtocol
from ..core.models import PriorCodexReviewComment, ReviewThreadSnapshot

SUMMARY_MARKER = "Codex Autonomous Review:"
_CURRENT_CODE_BLOCK_RE = re.compile(r"\*\*Current code:\*\*\s*```[^\n]*\n(.*?)```", re.DOTALL)


def has_prior_codex_review(
    reviews: Sequence[ReviewLikeProtocol],
    issue_comments: Sequence[IssueCommentLikeProtocol],
) -> bool:
    for review in reviews:
        review_body = review.body
        if isinstance(review_body, str) and SUMMARY_MARKER in review_body:
            return True

    return bool(collect_codex_author_logins(issue_comments))


def collect_codex_author_logins(
    issue_comments: Sequence[IssueCommentLikeProtocol],
) -> set[str]:
    author_logins: set[str] = set()
    for issue_comment in issue_comments:
        body = issue_comment.body
        if not isinstance(body, str) or SUMMARY_MARKER not in body:
            continue
        if issue_comment.user is None:
            continue
        author_login = issue_comment.user.login
        if isinstance(author_login, str) and author_login:
            author_logins.add(_normalize_author_login(author_login))
    return author_logins


def collect_prior_codex_review_comments(
    review_threads: Sequence[ReviewThreadSnapshot],
    codex_author_logins: set[str],
    repo_root: Path,
) -> list[PriorCodexReviewComment]:
    items: list[PriorCodexReviewComment] = []
    if not codex_author_logins:
        return items

    resolved_repo_root = repo_root.resolve()
    for review_thread in review_threads:
        if review_thread.is_resolved or not review_thread.comments:
            continue

        first_comment = review_thread.comments[0]
        prompt_line = first_comment.prompt_line
        if _normalize_author_login(first_comment.author) not in codex_author_logins:
            continue
        if not first_comment.body or not first_comment.path or not isinstance(prompt_line, int):
            continue
        current_code = _extract_current_code_block(first_comment.body)
        if current_code is None:
            continue
        items.append(
            PriorCodexReviewComment(
                id=first_comment.id,
                thread_id=review_thread.id,
                path=first_comment.path,
                line=prompt_line,
                body=first_comment.body,
                current_code=current_code,
                is_currently_applicable=_current_code_matches_file(
                    resolved_repo_root,
                    first_comment.path,
                    current_code,
                ),
            )
        )

    return items


def render_prior_codex_comments_for_prompt(
    existing_comments: Sequence[PriorCodexReviewComment],
) -> str:
    if not existing_comments:
        return ""

    applicable_comments = [
        comment for comment in existing_comments if comment.is_currently_applicable
    ]
    lines: list[str] = []
    if applicable_comments:
        lines.append("<prior_codex_review_comments>")
        for comment in applicable_comments[:200]:
            lines.append(
                json.dumps(
                    {
                        "id": comment.id,
                        "thread_id": comment.thread_id,
                        "path": comment.path,
                        "line": comment.line,
                        "current_code": comment.current_code,
                        "body": comment.body,
                    },
                    ensure_ascii=True,
                )
            )
        lines.append("</prior_codex_review_comments>")
    return "\n".join(lines)


def _extract_current_code_block(body: str) -> str | None:
    match = _CURRENT_CODE_BLOCK_RE.search(body)
    if match is None:
        return None
    current_code = match.group(1).strip()
    return current_code or None


def _current_code_matches_file(repo_root: Path, relative_path: str, current_code: str) -> bool:
    repo_file = _resolve_repo_file(repo_root, relative_path)
    if repo_file is None:
        return False
    try:
        file_text = repo_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return current_code.strip() in file_text


def _resolve_repo_file(repo_root: Path, relative_path: str) -> Path | None:
    try:
        repo_file = (repo_root / relative_path).resolve()
    except OSError:
        return None
    try:
        repo_file.relative_to(repo_root)
    except ValueError:
        return None
    if not repo_file.is_file():
        return None
    return repo_file


def _normalize_author_login(author_login: str) -> str:
    normalized = author_login.strip()
    if normalized.endswith("[bot]"):
        return normalized[:-5]
    return normalized
