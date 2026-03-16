from __future__ import annotations

from typing import Protocol

from ..core.filesystem import write_text_atomic
from ..core.github_types import (
    IssueCommentLikeProtocol,
    ReviewCommentLikeProtocol,
    UserLikeProtocol,
)
from ..core.models import IssueCommentSnapshot, ReviewCommentSnapshot
from .artifacts import ReviewArtifacts


class ReviewMetadataPullRequestProtocol(Protocol):
    number: int
    title: str
    body: str | None
    html_url: str
    state: str
    user: UserLikeProtocol | None


class ReviewContextWriter:
    """Writes review context artifacts for code review operations."""

    def write_context_artifacts(
        self,
        pr: ReviewMetadataPullRequestProtocol,
        artifacts: ReviewArtifacts,
        *,
        issue_comments: list[IssueCommentLikeProtocol],
        review_comments: list[ReviewCommentLikeProtocol],
    ) -> None:
        """Create a context directory with PR metadata and discussion context."""
        artifacts.base_dir.mkdir(parents=True, exist_ok=True)

        self._write_pr_metadata(pr, artifacts)
        self._write_review_comments(
            artifacts,
            issue_comments=issue_comments,
            review_comments=review_comments,
        )

    def _write_pr_metadata(
        self,
        pr: ReviewMetadataPullRequestProtocol,
        artifacts: ReviewArtifacts,
    ) -> None:
        """Write high-level PR metadata into pr.md."""
        parts: list[str] = []
        parts.append(f"PR #{pr.number}: {pr.title or ''}")
        parts.append("")
        parts.append(f"URL: {pr.html_url}")
        parts.append(f"Author: {pr.user.login if pr.user else ''}")
        parts.append(f"State: {pr.state}")
        parts.append("")
        body = pr.body or ""
        if body:
            parts.append("PR Description:\n")
            parts.append(body)
            parts.append("")

        write_text_atomic(artifacts.pr_metadata_path, "\n".join(parts) + "\n")

    def _write_review_comments(
        self,
        artifacts: ReviewArtifacts,
        *,
        issue_comments: list[IssueCommentLikeProtocol],
        review_comments: list[ReviewCommentLikeProtocol],
    ) -> None:
        """Write issue-level and inline review comments to review_comments.md."""
        lines: list[str] = []
        lines.extend(self._render_issue_comment_lines(issue_comments))
        lines.extend(self._render_inline_review_comment_lines(review_comments))

        if not lines:
            lines.append("(no review comments available)")

        write_text_atomic(artifacts.review_comments_path, "\n".join(lines) + "\n")

    def _render_issue_comment_lines(
        self,
        issue_comments: list[IssueCommentLikeProtocol],
    ) -> list[str]:
        if not issue_comments:
            return []

        lines: list[str] = ["Issue Comments:"]
        skipped_issue_comments = 0
        for comment in issue_comments:
            rendered = _render_issue_comment(comment)
            if rendered is None:
                skipped_issue_comments += 1
                continue
            lines.extend(rendered)
        if skipped_issue_comments > 0:
            lines.append(f"(skipped {skipped_issue_comments} issue comment(s) with invalid shape)")
            lines.append("")
        return lines

    def _render_inline_review_comment_lines(
        self,
        review_comments: list[ReviewCommentLikeProtocol],
    ) -> list[str]:
        if not review_comments:
            return []

        lines: list[str] = ["Inline Review Comments:"]
        skipped_review_comments = 0
        for review_comment in review_comments:
            rendered = _render_review_comment(review_comment)
            if rendered is None:
                skipped_review_comments += 1
                continue
            lines.extend(rendered)
        if skipped_review_comments > 0:
            lines.append(
                f"(skipped {skipped_review_comments} inline review comment(s) with invalid shape)"
            )
            lines.append("")
        return lines


def _render_issue_comment(comment: IssueCommentLikeProtocol) -> list[str] | None:
    snapshot = IssueCommentSnapshot.from_issue_comment(comment)
    if not snapshot.body or not snapshot.created_at:
        return None
    return [f"- [{snapshot.created_at}] @{snapshot.author}:", snapshot.body, ""]


def _render_review_comment(comment: ReviewCommentLikeProtocol) -> list[str] | None:
    snapshot = ReviewCommentSnapshot.from_review_comment(comment)
    prompt_line = snapshot.prompt_line
    if not snapshot.created_at or not snapshot.path or prompt_line is None or prompt_line <= 0:
        return None
    return [
        f"- [{snapshot.created_at}] @{snapshot.author} on {snapshot.path}:{prompt_line}",
        snapshot.body,
        "",
    ]
