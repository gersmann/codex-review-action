from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from ..core.config import ReviewConfig
from ..core.github_types import BaseRefLikeProtocol, HeadRefLikeProtocol
from ..core.models import (
    CommentContext,
    ReviewCommentSnapshot,
    UnresolvedReviewComment,
    UnresolvedReviewThread,
)

CommentContextStatus = Literal["available", "unavailable", "degraded"]
UNRESOLVED_COMMENTS_HEADER = (
    "<unresolved_comments>\n"
    "These are the UNRESOLVED review threads. For each, make the smallest code change that addresses the feedback.\n"
    "Do not mark threads resolved; just apply code fixes.\n"
)


class PromptPullRequestProtocol(Protocol):
    head: HeadRefLikeProtocol | None
    base: BaseRefLikeProtocol | None


@dataclass(frozen=True)
class CommentContextRenderResult:
    block: str = ""
    warning: str | None = None
    status: CommentContextStatus = "unavailable"

    @property
    def is_degraded(self) -> bool:
        return self.status == "degraded" or self.warning is not None


def build_edit_prompt(
    config: ReviewConfig,
    command_text: str,
    pr: PromptPullRequestProtocol,
    comment_context_block: str,
    unresolved_block: str,
) -> str:
    repo_root = config.resolved_repo_root
    head_ref = "HEAD"
    base_ref = "main"

    head = pr.head
    if head and isinstance(head.ref, str) and head.ref:
        head_ref = head.ref

    base = pr.base
    if base and isinstance(base.ref, str) and base.ref:
        base_ref = base.ref

    sections: list[str] = [
        (
            "<act_overview>\n"
            "You are a coding agent with write access to this repository.\n"
            f"Repository root: {repo_root}\n"
            "Make the requested change with the smallest reasonable diff.\n"
            "Use the apply_patch tool to edit files. Create files/dirs if needed.\n"
            "Do not change unrelated code.\n"
            "</act_overview>\n"
        )
    ]

    extra = config.act_instructions.strip()
    if extra:
        sections.append("<extra_instructions>\n" + extra + "\n</extra_instructions>\n")

    sections.append(
        f"<pr_context>\n<head>{head_ref}</head>\n<base>{base_ref}</base>\n</pr_context>\n"
    )

    if unresolved_block:
        sections.append(unresolved_block)

    if comment_context_block:
        sections.append(comment_context_block)

    sections.append("<edit_request>\n" + command_text.strip() + "\n</edit_request>\n")
    sections.append(
        "<completion_rules>\n"
        "- Apply the change and ensure the project still type-checks/builds if applicable.\n"
        "- Keep diffs minimal and focused on the request.\n"
        "- The host workflow handles git commit/push after your edits; do not run git commit or git push.\n"
        "</completion_rules>\n"
    )

    return "".join(sections)


def format_unresolved_threads_from_list(threads: Sequence[UnresolvedReviewThread]) -> str:
    items = [
        rendered
        for thread in threads
        if (rendered := _render_unresolved_thread(thread)) is not None
    ]
    if not items:
        return ""
    return UNRESOLVED_COMMENTS_HEADER + "\n".join(items) + "\n</unresolved_comments>\n"


def _render_unresolved_thread(thread: UnresolvedReviewThread) -> str | None:
    if not thread.comments:
        return None
    entry_lines: list[str] = [f'<thread id="{thread.id}">']
    entry_lines.extend(_render_unresolved_comment(comment) for comment in thread.comments)
    entry_lines.append("</thread>")
    return "\n".join(entry_lines)


def _render_unresolved_comment(comment: UnresolvedReviewComment) -> str:
    line = comment.prompt_line or ""
    body = comment.body.strip()
    return (
        f'<comment id="{comment.id}" author="{comment.author}" path="{comment.path}" '
        f'line="{line}">\n{body}\n</comment>'
    )


def build_comment_context_block(
    config: ReviewConfig,
    comment_ctx: CommentContext | None,
    *,
    review_comment_snapshot: ReviewCommentSnapshot | None = None,
    parent_review_comment_snapshot: ReviewCommentSnapshot | None = None,
    lookup_warning: str | None = None,
) -> CommentContextRenderResult:
    if not comment_ctx:
        return CommentContextRenderResult()

    event = comment_ctx.event_name.lower()
    comment_id = comment_ctx.id
    if not comment_id:
        return CommentContextRenderResult()

    if event == "pull_request_review_comment":
        return _build_review_comment_context_block(
            config,
            comment_id,
            review_comment_snapshot=review_comment_snapshot,
            parent_review_comment_snapshot=parent_review_comment_snapshot,
            lookup_warning=lookup_warning,
        )

    if event == "issue_comment":
        body = comment_ctx.body
        return CommentContextRenderResult(
            block=(
                '<comment_context type="issue_comment">\n'
                f"<id>{comment_id}</id>\n"
                "<note>No file/line associated with this comment. If the edit targets a specific file, infer from the repository structure or the instruction text.</note>\n"
                "<body>\n" + body + "\n</body>\n"
                "</comment_context>\n"
            ),
            status="available",
        )

    return CommentContextRenderResult()


def _build_review_comment_context_block(
    config: ReviewConfig,
    comment_id: int,
    *,
    review_comment_snapshot: ReviewCommentSnapshot | None,
    parent_review_comment_snapshot: ReviewCommentSnapshot | None,
    lookup_warning: str | None,
) -> CommentContextRenderResult:
    review_comment = review_comment_snapshot
    if review_comment is None:
        return CommentContextRenderResult(
            warning=lookup_warning
            or f"Comment context lookup failed for review comment {comment_id}",
            status="degraded",
        )

    path = review_comment.path
    line = review_comment.line
    original_line = review_comment.original_line
    warnings: list[str] = []
    path, line, original_line = _apply_parent_context_fallback(
        parent_review_comment_snapshot=parent_review_comment_snapshot,
        path=path,
        line=line,
        original_line=original_line,
    )

    excerpt = _render_excerpt_block(
        config=config,
        path=path,
        line=line,
        original_line=original_line,
        warnings=warnings,
    )
    warning_text = " | ".join(warnings) if warnings else None
    warning_block = ""
    if warning_text:
        warning_block = "<context_warnings>\n" + warning_text + "\n</context_warnings>\n"

    block = (
        '<comment_context type="pull_request_review_comment">\n'
        f"<id>{comment_id}</id>\n"
        f"<path>{path}</path>\n"
        f"<line>{line or ''}</line>\n"
        f"<original_line>{original_line or ''}</original_line>\n"
        f"<commit>{review_comment.commit_id}</commit>\n"
        "<diff_hunk>\n"
        + review_comment.diff_hunk
        + "\n</diff_hunk>\n"
        + warning_block
        + excerpt
        + "</comment_context>\n"
    )
    return CommentContextRenderResult(
        block=block,
        warning=(
            f"Continuing with degraded comment context for review comment {comment_id}: "
            f"{warning_text}"
            if warning_text
            else None
        ),
        status="degraded" if warning_text else "available",
    )


def _apply_parent_context_fallback(
    *,
    parent_review_comment_snapshot: ReviewCommentSnapshot | None,
    path: str,
    line: int | None,
    original_line: int | None,
) -> tuple[str, int | None, int | None]:
    needs_fallback = not path or (line is None and original_line is None)
    if not needs_fallback:
        return path, line, original_line
    if parent_review_comment_snapshot is None:
        return path, line, original_line

    resolved_path = path if path else parent_review_comment_snapshot.path
    resolved_line = line if line is not None else parent_review_comment_snapshot.line
    resolved_original_line = (
        original_line if original_line is not None else parent_review_comment_snapshot.original_line
    )
    return resolved_path, resolved_line, resolved_original_line


def _render_excerpt_block(
    *,
    config: ReviewConfig,
    path: str,
    line: int | None,
    original_line: int | None,
    warnings: list[str],
) -> str:
    if not path:
        return ""
    focus_line = line or original_line or 0
    try:
        return read_file_excerpt(config, path, focus_line)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to read file excerpt for %s:%s while building comment context",
            path,
            focus_line,
            exc_info=exc,
        )
        warnings.append(f"Failed to read file excerpt for {path}:{focus_line}: {exc}")
        return ""


def read_file_excerpt(
    config: ReviewConfig, rel_path: str, focus_line: int, context: int = 30
) -> str:
    if not rel_path:
        return ""

    repo_root = config.resolved_repo_root
    abs_path = (repo_root / rel_path).resolve()
    text = abs_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    total = len(lines)

    if focus_line <= 0:
        start = 1
        end = min(total, 2 * context)
    else:
        start = max(1, focus_line - context)
        end = min(total, focus_line + context)

    buffer = [f'<file_excerpt path="{rel_path}" start="{start}" end="{end}">\n']
    for line_number in range(start, end + 1):
        code = lines[line_number - 1]
        buffer.append(f"{line_number:>6}: {code}")
    buffer.append("\n</file_excerpt>\n")
    return "\n".join(buffer)
