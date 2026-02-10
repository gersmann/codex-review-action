from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ReviewConfig
from .models import CommentContext


def build_edit_prompt(
    config: ReviewConfig,
    command_text: str,
    pr: Any,
    comment_ctx: CommentContext | None,
    unresolved_block: str,
) -> str:
    repo_root = config.repo_root or Path(".").resolve()
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

    comment_context = format_comment_context(config, pr, comment_ctx)
    if comment_context:
        sections.append(comment_context)

    sections.append("<edit_request>\n" + command_text.strip() + "\n</edit_request>\n")
    sections.append(
        "<completion_rules>\n"
        "- Apply the change and ensure the project still type-checks/builds if applicable.\n"
        "- Keep diffs minimal and focused on the request.\n"
        "- If code changes were performed, commit those changes and push the branch.\n"
        "- If no code changes were needed, do not create an empty commit.\n"
        "</completion_rules>\n"
    )

    return "".join(sections)


def format_unresolved_threads_from_list(threads: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for thread in threads:
        try:
            thread_id = thread.get("id") or thread.get("node_id") or ""
            comments_value = thread.get("comments")
            if not isinstance(comments_value, list):
                continue

            entry_lines: list[str] = [f'<thread id="{thread_id}">']
            for comment in comments_value:
                if not isinstance(comment, dict):
                    continue
                comment_id = comment.get("id") or ""
                user = comment.get("user")
                author = ""
                if isinstance(user, dict):
                    author_value = user.get("login")
                    author = str(author_value) if author_value else ""
                path = str(comment.get("path") or "")
                line = comment.get("line") or comment.get("original_line") or ""
                body = str(comment.get("body") or "").strip()
                entry_lines.append(
                    f'<comment id="{comment_id}" author="{author}" path="{path}" line="{line}">\n{body}\n</comment>'
                )

            entry_lines.append("</thread>")
            items.append("\n".join(entry_lines))
        except Exception:
            continue

    if not items:
        return ""

    header = (
        "<unresolved_comments>\n"
        "These are the UNRESOLVED review threads. For each, make the smallest code change that addresses the feedback.\n"
        "Do not mark threads resolved; just apply code fixes.\n"
    )
    return header + "\n".join(items) + "\n</unresolved_comments>\n"


def format_comment_context(
    config: ReviewConfig,
    pr: Any,
    comment_ctx: CommentContext | None,
) -> str:
    if not comment_ctx:
        return ""

    event = comment_ctx.event_name.lower()
    comment_id = comment_ctx.id
    if not comment_id:
        return ""

    try:
        if event == "pull_request_review_comment":
            review_comment = pr.get_review_comment(comment_id)

            path = review_comment.path or ""
            line = review_comment.line
            original_line = review_comment.original_line
            in_reply_to_id = review_comment.in_reply_to_id

            if (not path or (line is None and original_line is None)) and in_reply_to_id:
                try:
                    parent = pr.get_review_comment(int(in_reply_to_id))
                    if not path:
                        path = parent.path or ""
                    if line is None:
                        line = parent.line
                    if original_line is None:
                        original_line = parent.original_line
                except Exception:
                    pass

            diff_hunk = review_comment.diff_hunk or ""
            commit_id = review_comment.commit_id or ""
            excerpt = read_file_excerpt(config, path, line or original_line or 0)

            return (
                '<comment_context type="pull_request_review_comment">\n'
                f"<id>{comment_id}</id>\n"
                f"<path>{path}</path>\n"
                f"<line>{line or ''}</line>\n"
                f"<original_line>{original_line or ''}</original_line>\n"
                f"<commit>{commit_id}</commit>\n"
                "<diff_hunk>\n" + diff_hunk + "\n</diff_hunk>\n" + excerpt + "</comment_context>\n"
            )

        if event == "issue_comment":
            body = comment_ctx.body
            return (
                '<comment_context type="issue_comment">\n'
                f"<id>{comment_id}</id>\n"
                "<note>No file/line associated with this comment. If the edit targets a specific file, infer from the repository structure or the instruction text.</note>\n"
                "<body>\n" + body + "\n</body>\n"
                "</comment_context>\n"
            )
    except Exception:
        return ""

    return ""


def read_file_excerpt(
    config: ReviewConfig, rel_path: str, focus_line: int, context: int = 30
) -> str:
    if not rel_path:
        return ""

    try:
        repo_root = config.repo_root or Path(".").resolve()
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
    except Exception:
        return ""
