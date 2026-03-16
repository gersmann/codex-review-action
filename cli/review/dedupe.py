from __future__ import annotations

import json
from collections.abc import Sequence

from ..core.github_types import IssueCommentLikeProtocol, ReviewLikeProtocol
from ..core.models import ExistingReviewComment, ReviewThreadSnapshot

SUMMARY_MARKER = "Codex Autonomous Review:"


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
            author_logins.add(author_login)
    return author_logins


def collect_prior_codex_review_comments(
    review_threads: Sequence[ReviewThreadSnapshot],
    codex_author_logins: set[str],
) -> list[ExistingReviewComment]:
    items: list[ExistingReviewComment] = []
    if not codex_author_logins:
        return items

    for review_thread in review_threads:
        if review_thread.is_resolved or not review_thread.comments:
            continue

        first_comment = review_thread.comments[0]
        prompt_line = first_comment.prompt_line
        if first_comment.author not in codex_author_logins:
            continue
        if not first_comment.body or not first_comment.path or not isinstance(prompt_line, int):
            continue

        items.append(
            ExistingReviewComment(
                id=first_comment.id,
                path=first_comment.path,
                line=prompt_line,
                body=first_comment.body,
            )
        )

    return items


def render_prior_codex_comments_for_prompt(
    existing_comments: Sequence[ExistingReviewComment],
) -> str:
    if not existing_comments:
        return ""

    lines = ["<prior_codex_review_comments>"]
    for comment in existing_comments[:200]:
        lines.append(
            json.dumps(
                {
                    "id": comment.id,
                    "path": comment.path,
                    "line": comment.line,
                    "body": comment.body,
                },
                ensure_ascii=True,
            )
        )
    lines.append("</prior_codex_review_comments>")
    return "\n".join(lines)
