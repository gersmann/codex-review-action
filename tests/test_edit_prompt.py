from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.core.config import ReviewConfig
from cli.core.models import (
    CommentContext,
    ReviewCommentSnapshot,
    UnresolvedReviewComment,
    UnresolvedReviewThread,
)
from cli.workflows.edit_prompt import (
    build_comment_context_block,
    build_edit_prompt,
    format_unresolved_threads_from_list,
)


class _Ref:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class _PR:
    def __init__(self) -> None:
        self.head = _Ref("feature/test-branch")
        self.base = _Ref("main")


def test_build_edit_prompt_includes_git_ownership_completion_rules() -> None:
    config = ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="act",
    )
    prompt = build_edit_prompt(
        config=config,
        command_text="/codex update README wording",
        pr=cast(Any, _PR()),
        comment_context_block="",
        unresolved_block="",
    )

    assert "<completion_rules>" in prompt
    assert "do not run git commit or git push" in prompt


def test_format_unresolved_threads_skips_non_list_comments() -> None:
    rendered = format_unresolved_threads_from_list(
        [
            UnresolvedReviewThread(id="thread-1", comments=[]),
            UnresolvedReviewThread(
                id="thread-2",
                comments=[
                    UnresolvedReviewComment(
                        id="comment-1",
                        body=" fix me ",
                        path="a.py",
                        line=7,
                        original_line=None,
                    )
                ],
            ),
        ]
    )

    assert '<thread id="thread-2">' in rendered
    assert "fix me" in rendered
    assert "thread-1" not in rendered


def test_build_comment_context_block_uses_parent_review_comment(tmp_path: Path) -> None:
    config = ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="act",
        repo_root=tmp_path,
    )
    file_path = tmp_path / "pkg" / "mod.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    rendered = build_comment_context_block(
        config,
        CommentContext(id=9, event_name="pull_request_review_comment"),
        review_comment_snapshot=ReviewCommentSnapshot(
            body="",
            path="",
            line=None,
            original_line=None,
            author="",
            created_at="now",
            diff_hunk="@@ -1,1 +1,1 @@",
            commit_id="deadbeef",
            in_reply_to_id=10,
        ),
        parent_review_comment_snapshot=ReviewCommentSnapshot(
            body="",
            path="pkg/mod.py",
            line=2,
            original_line=2,
            author="",
            created_at="now",
            diff_hunk="@@ -1,1 +1,1 @@",
            commit_id="deadbeef",
            in_reply_to_id=None,
        ),
    )

    assert "<path>pkg/mod.py</path>" in rendered.block
    assert '<file_excerpt path="pkg/mod.py"' in rendered.block
    assert rendered.warning is None
    assert rendered.status == "available"


def test_build_comment_context_block_reports_excerpt_failures(tmp_path: Path) -> None:
    config = ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="act",
        repo_root=tmp_path,
    )

    rendered = build_comment_context_block(
        config,
        CommentContext(id=9, event_name="pull_request_review_comment"),
        review_comment_snapshot=ReviewCommentSnapshot(
            body="",
            path="missing.py",
            line=2,
            original_line=2,
            author="",
            created_at="now",
            diff_hunk="@@ -1,1 +1,1 @@",
            commit_id="deadbeef",
            in_reply_to_id=None,
        ),
    )

    assert rendered.block
    assert "<path>missing.py</path>" in rendered.block
    assert rendered.warning is not None
    assert rendered.status == "degraded"
    assert "<context_warnings>" in rendered.block
    assert "Failed to read file excerpt for missing.py:2" in rendered.warning
