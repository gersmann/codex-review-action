from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from cli.clients.github_client import GitHubClient, _extract_review_threads_page, _normalize_comment
from cli.core.config import ReviewConfig
from cli.core.exceptions import ReviewContractError
from cli.core.filesystem import write_text_atomic
from cli.core.github_types import IssueCommentLikeProtocol, ReviewCommentLikeProtocol
from cli.core.models import (
    CommentContext,
    FindingLocation,
    InlineCommentPayload,
    PriorCodexReviewComment,
    ReviewFinding,
    ReviewRunResult,
    ReviewThreadComment,
    ReviewThreadSnapshot,
)
from cli.main import extract_edit_command, load_github_event
from cli.review.anchor_engine import RangeAnchor, build_anchor_maps, resolve_range
from cli.review.artifacts import ReviewArtifacts
from cli.review.context_manager import ReviewContextWriter
from cli.review.dedupe import (
    SUMMARY_MARKER,
    collect_codex_author_logins,
    collect_prior_codex_review_comments,
    has_prior_codex_review,
    render_prior_codex_comments_for_prompt,
)
from cli.review.patch_parser import (
    ParsedPatch,
    annotate_patch_with_line_numbers,
    parse_patch,
    to_relative_path,
)
from cli.review.posting import (
    build_inline_comment_payloads,
    persist_anchor_maps,
    post_inline_comments,
)
from cli.workflows.edit_workflow import EditWorkflow, _format_edit_reply


class _FakeChangedFile:
    def __init__(self, filename: str | None, patch: str | None) -> None:
        self.filename = filename
        self.patch = patch


class _FakeUser:
    def __init__(self, login: str) -> None:
        self.login: str = login


class _FakeIssueComment:
    def __init__(
        self,
        body: str,
        *,
        comment_id: int = 1,
        login: str = "alice",
        created_at: str = "now",
    ) -> None:
        self.body: str | None = body
        self.id = comment_id
        self.user = _FakeUser(login)
        self.created_at: object = created_at
        self.deleted = False

    def delete(self) -> None:
        self.deleted = True


class _FakePullRequestComment:
    def __init__(
        self,
        body: str,
        *,
        path: str = "sample.py",
        line: int | None = 5,
        original_line: int | None = 4,
        login: str = "bob",
        created_at: str = "later",
    ) -> None:
        self.body: str | None = body
        self.path: str | None = path
        self.line = line
        self.original_line = original_line
        self.in_reply_to_id: int | None = None
        self.diff_hunk: str | None = None
        self.commit_id: str | None = None
        self.user = _FakeUser(login)
        self.created_at: object = created_at


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def requestJsonAndCheck(  # noqa: N802
        self, method: str, url: str, input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, url, input))
        return {}

    def graphql_query(
        self, query: str, variables: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(("GRAPHQL", query, variables))
        return {}, {"data": {"resolveReviewThread": {"thread": {"id": variables["threadId"]}}}}


class _FakeIssue:
    def __init__(self) -> None:
        self.comments: list[str] = []

    def create_comment(self, text: str) -> None:
        self.comments.append(text)


class _FakePR:
    def __init__(self) -> None:
        self.url = "https://api.example.test/pulls/1"
        self._requester = _FakeRequester()
        self._issue = _FakeIssue()
        self.number = 1
        self.title = "Improve tests"
        self.html_url = "https://example.test/pr/1"
        self.user = _FakeUser("octocat")
        self.state = "open"
        self.body = "Body text"
        self._issue_comments: list[Any] = []
        self._review_comments: list[Any] = []

    def as_issue(self) -> _FakeIssue:
        return self._issue

    def get_issue_comments(self) -> list[Any]:
        return list(self._issue_comments)

    def get_review_comments(self) -> list[Any]:
        return list(self._review_comments)


def _make_edit_workflow() -> EditWorkflow:
    from cli.core.config import ReviewConfig

    return EditWorkflow(ReviewConfig(github_token="t", repository="o/r", pr_number=1, mode="act"))


def test_patch_parser_and_anchor_engine_cover_common_cases(tmp_path: Path) -> None:
    patch = "@@ -1,2 +1,3 @@\n line1\n-line2\n+line2 changed\n+line3\n"
    parsed = parse_patch(patch)

    assert parsed.valid_head_lines == {1, 2, 3}
    assert parsed.added_head_lines == {2, 3}
    assert parsed.content_by_head_line[2] == "line2 changed"
    assert parsed.positions_by_head_line[3] == 4
    assert parsed.hunks == [(1, 3)]

    annotated = annotate_patch_with_line_numbers(patch)
    assert "@@ -1,2 +1,3 @@" in annotated
    assert "     2   +  line2 changed" in annotated

    repo_root = tmp_path
    assert (
        to_relative_path(str((repo_root / "pkg" / "mod.py").resolve()), repo_root) == "pkg/mod.py"
    )
    assert to_relative_path("/tmp/outside.py", repo_root) == "tmp/outside.py"

    anchor_maps = build_anchor_maps(
        cast(
            Any,
            [
                _FakeChangedFile("sample.py", patch),
                _FakeChangedFile("skip.py", None),
                _FakeChangedFile(None, patch),
            ],
        )
    )
    assert set(anchor_maps) == {"sample.py"}

    file_map = ParsedPatch(
        valid_head_lines={1, 2, 3},
        added_head_lines={1, 2, 3},
        content_by_head_line={1: "one", 2: "two", 3: "three"},
        hunks=[(1, 3)],
    )
    assert resolve_range(1, 2, True, file_map) == RangeAnchor(
        kind="range",
        start_line=1,
        end_line=2,
    )
    assert resolve_range(0, 1, False, file_map) is None


def test_write_text_atomic_overwrites_and_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.txt"
    write_text_atomic(target, "first")
    assert target.read_text(encoding="utf-8") == "first"

    write_text_atomic(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_model_helpers_parse_and_normalize_payloads() -> None:
    assert CommentContext.from_mapping(None) is None
    assert CommentContext.from_mapping({"id": "bad", "event_name": 1, "author": None}) is None

    finding = {
        "code_location": {
            "absolute_file_path": " /tmp/a.py ",
            "line_range": {"start": "4", "end": 0},
        }
    }
    assert FindingLocation.from_finding(finding) == FindingLocation("/tmp/a.py", 4, 4)
    assert (
        FindingLocation.from_finding(
            {"code_location": {"absolute_file_path": "", "line_range": {}}}
        )
        is None
    )

    payload = InlineCommentPayload(body="body", path="a.py", line=7, start_line=5)
    assert payload.to_request_payload("deadbeef") == {
        "body": "body",
        "path": "a.py",
        "side": "RIGHT",
        "commit_id": "deadbeef",
        "line": 7,
        "start_line": 5,
        "start_side": "RIGHT",
    }

    result = ReviewRunResult.from_payload(
        {
            "overall_correctness": "ok",
            "overall_explanation": "fine",
            "overall_confidence_score": 0.75,
            "carried_forward": [],
            "resolved_comment_ids": [],
            "findings": [
                {
                    "title": "a",
                    "body": "",
                    "confidence_score": None,
                    "priority": None,
                    "code_location": {
                        "absolute_file_path": "/tmp/a.py",
                        "line_range": {"start": 1, "end": 1},
                    },
                }
            ],
        }
    )
    assert result.as_dict() == {
        "overall_correctness": "ok",
        "overall_explanation": "fine",
        "overall_confidence_score": 0.75,
        "carried_forward": [],
        "resolved_comment_ids": [],
        "findings": [
            {
                "title": "a",
                "body": "",
                "confidence_score": None,
                "priority": None,
                "code_location": {
                    "absolute_file_path": "/tmp/a.py",
                    "line_range": {"start": 1, "end": 1},
                },
            }
        ],
    }

    with pytest.raises(ReviewContractError, match="finding at index 1 must be an object"):
        ReviewRunResult.from_payload(
            {
                "overall_correctness": "ok",
                "overall_explanation": "fine",
                "overall_confidence_score": 0.75,
                "carried_forward": [],
                "resolved_comment_ids": [],
                "findings": [
                    {
                        "title": "a",
                        "body": "",
                        "confidence_score": None,
                        "priority": None,
                        "code_location": {
                            "absolute_file_path": "/tmp/a.py",
                            "line_range": {"start": 1, "end": 1},
                        },
                    },
                    "ignore-me",
                ],
            }
        )


def test_review_context_writer_writes_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _FakePR()
    pr._issue_comments = [_FakeIssueComment("issue body")]
    pr._review_comments = [_FakePullRequestComment("inline body", path="pkg/mod.py", line=9)]

    artifacts = ReviewArtifacts(repo_root=tmp_path, context_dir_name=".codex-context")
    ReviewContextWriter().write_context_artifacts(
        cast(Any, pr),
        artifacts,
        issue_comments=cast(list[IssueCommentLikeProtocol], pr._issue_comments),
        review_comments=cast(list[ReviewCommentLikeProtocol], pr._review_comments),
    )

    pr_md = artifacts.pr_metadata_path.read_text(encoding="utf-8")
    review_md = artifacts.review_comments_path.read_text(encoding="utf-8")

    assert "PR #1: Improve tests" in pr_md
    assert "Issue Comments:" in review_md
    assert "Inline Review Comments:" in review_md
    assert "pkg/mod.py:9" in review_md


def test_review_context_writer_uses_provided_discussion_snapshots(tmp_path: Path) -> None:
    class _BrokenPR(_FakePR):
        def get_issue_comments(self) -> list[Any]:
            raise RuntimeError("issue fetch failed")

        def get_review_comments(self) -> list[Any]:
            raise RuntimeError("review fetch failed")

    artifacts = ReviewArtifacts(repo_root=tmp_path, context_dir_name=".codex-context")
    ReviewContextWriter().write_context_artifacts(
        cast(Any, _BrokenPR()),
        artifacts,
        issue_comments=cast(list[IssueCommentLikeProtocol], [_FakeIssueComment("issue body")]),
        review_comments=cast(
            list[ReviewCommentLikeProtocol],
            [_FakePullRequestComment("inline body", path="pkg/mod.py", line=9)],
        ),
    )

    review_md = artifacts.review_comments_path.read_text(encoding="utf-8")

    assert "Issue Comments:" in review_md
    assert "Inline Review Comments:" in review_md
    assert "issue body" in review_md
    assert "inline body" in review_md
    assert "(no review comments available)" not in review_md


def test_review_context_writer_marks_invalid_comment_shapes(tmp_path: Path) -> None:
    class _InvalidIssueComment:
        body = None
        created_at = "now"
        id = 1
        user = None

        def delete(self) -> None:
            return None

    class _InvalidReviewComment:
        body = "body"
        path = ""
        line = None
        original_line = None
        in_reply_to_id = None
        diff_hunk = "@@ -1 +1 @@"
        commit_id = "deadbeef"
        created_at = "now"
        user = None

    pr = _FakePR()
    pr._issue_comments = [_InvalidIssueComment()]
    pr._review_comments = [_InvalidReviewComment()]

    artifacts = ReviewArtifacts(repo_root=tmp_path, context_dir_name=".codex-context")
    ReviewContextWriter().write_context_artifacts(
        cast(Any, pr),
        artifacts,
        issue_comments=cast(list[IssueCommentLikeProtocol], pr._issue_comments),
        review_comments=cast(list[ReviewCommentLikeProtocol], pr._review_comments),
    )

    review_md = artifacts.review_comments_path.read_text(encoding="utf-8")

    assert "(skipped 1 issue comment(s) with invalid shape)" in review_md
    assert "(skipped 1 inline review comment(s) with invalid shape)" in review_md
    assert "(no review comments available)" not in review_md


def test_review_dedupe_helpers(tmp_path: Path) -> None:
    assert has_prior_codex_review([type("R", (), {"body": SUMMARY_MARKER})()], []) is True
    assert (
        has_prior_codex_review(
            [],
            cast(list[IssueCommentLikeProtocol], [_FakeIssueComment(f"{SUMMARY_MARKER} summary")]),
        )
        is True
    )

    issue_comments = cast(
        list[IssueCommentLikeProtocol],
        [
            _FakeIssueComment(f"{SUMMARY_MARKER}\nold summary", login="bot"),
            _FakeIssueComment("human note", login="alice"),
        ],
    )
    assert collect_codex_author_logins(issue_comments) == {"bot"}

    (tmp_path / "renamed.py").write_text("value = 1\n", encoding="utf-8")
    structured_body = (
        "**Current code:**\n```python\nvalue = 1\n```\n\n"
        "**Problem:** still broken.\n\n"
        "**Fix:**\n```python\nvalue = 1\n```\n\n---"
    )

    prior_codex_comments = collect_prior_codex_review_comments(
        [
            ReviewThreadSnapshot(
                id="thread-1",
                is_resolved=False,
                comments=[
                    ReviewThreadComment(
                        id="comment-1",
                        body=structured_body,
                        path="renamed.py",
                        line=11,
                        original_line=10,
                        author="bot",
                    )
                ],
            ),
            ReviewThreadSnapshot(
                id="thread-2",
                is_resolved=False,
                comments=[
                    ReviewThreadComment(
                        id="comment-2",
                        body=structured_body,
                        path="other.py",
                        line=7,
                        original_line=7,
                        author="alice",
                    )
                ],
            ),
            ReviewThreadSnapshot(
                id="thread-3",
                is_resolved=False,
                comments=[
                    ReviewThreadComment(
                        id="comment-3",
                        body=(
                            "**Current code:**\n```python\nvalue = 2\n```\n\n"
                            "**Problem:** stale.\n\n"
                            "**Fix:**\n```python\nvalue = 2\n```\n\n---"
                        ),
                        path="renamed.py",
                        line=5,
                        original_line=5,
                        author="bot",
                    )
                ],
            ),
            ReviewThreadSnapshot(
                id="thread-4",
                is_resolved=True,
                comments=[
                    ReviewThreadComment(
                        id="comment-4",
                        body="resolved body",
                        path="done.py",
                        line=5,
                        original_line=5,
                        author="bot",
                    )
                ],
            ),
        ],
        {"bot"},
        tmp_path,
    )
    assert prior_codex_comments == [
        PriorCodexReviewComment(
            id="comment-1",
            thread_id="thread-1",
            path="renamed.py",
            line=11,
            body=structured_body,
            current_code="value = 1",
            is_currently_applicable=True,
        ),
        PriorCodexReviewComment(
            id="comment-3",
            thread_id="thread-3",
            path="renamed.py",
            line=5,
            body=(
                "**Current code:**\n```python\nvalue = 2\n```\n\n"
                "**Problem:** stale.\n\n"
                "**Fix:**\n```python\nvalue = 2\n```\n\n---"
            ),
            current_code="value = 2",
            is_currently_applicable=False,
        ),
    ]
    assert render_prior_codex_comments_for_prompt(prior_codex_comments) == "\n".join(
        [
            "<prior_codex_review_comments>",
            '{"id": "comment-1", "thread_id": "thread-1", "path": "renamed.py", "line": 11, "current_code": "value = 1", "body": "**Current code:**\\n```python\\nvalue = 1\\n```\\n\\n**Problem:** still broken.\\n\\n**Fix:**\\n```python\\nvalue = 1\\n```\\n\\n---"}',
            "</prior_codex_review_comments>",
            "<prior_codex_review_comments_candidate_resolutions>",
            '{"id": "comment-3", "thread_id": "thread-3", "path": "renamed.py", "line": 5, "current_code": "value = 2", "body": "**Current code:**\\n```python\\nvalue = 2\\n```\\n\\n**Problem:** stale.\\n\\n**Fix:**\\n```python\\nvalue = 2\\n```\\n\\n---"}',
            "</prior_codex_review_comments_candidate_resolutions>",
        ]
    )


def test_review_posting_helpers_write_and_post(tmp_path: Path) -> None:
    repo_root = tmp_path
    artifacts = ReviewArtifacts(repo_root=repo_root, context_dir_name=".codex-context")
    file_map = ParsedPatch(
        valid_head_lines={1, 2, 3},
        added_head_lines={1, 2, 3},
        content_by_head_line={1: "alpha", 2: "beta", 3: "gamma"},
        positions_by_head_line={1: 1, 2: 2, 3: 3},
        hunks=[(1, 3)],
    )
    persist_anchor_maps({"sample.py": file_map}, artifacts)
    payload = json.loads(artifacts.anchor_maps_path.read_text("utf-8"))
    assert payload["sample.py"]["added_head_lines"] == [1, 2, 3]

    findings = [
        ReviewFinding.from_mapping(
            {
                "title": "Range",
                "body": "```suggestion\nnew\n```",
                "confidence_score": None,
                "priority": None,
                "code_location": {
                    "absolute_file_path": str((repo_root / "sample.py").resolve()),
                    "line_range": {"start": 1, "end": 2},
                },
            }
        ),
        ReviewFinding.from_mapping(
            {
                "title": "Single",
                "body": "```suggestion\none\n```",
                "confidence_score": None,
                "priority": None,
                "code_location": {
                    "absolute_file_path": str((repo_root / "sample.py").resolve()),
                    "line_range": {"start": 3, "end": 3},
                },
            }
        ),
    ]
    debug_messages: list[str] = []
    build_result = build_inline_comment_payloads(
        findings,
        {"sample.py": file_map},
        {},
        repo_root,
        dry_run=False,
        debug=lambda level, message: debug_messages.append(f"{level}:{message}"),
    )
    assert build_result.payloads[0].start_line == 1
    assert build_result.payloads[1].body.endswith("```diff\none\n```")
    assert build_result.dropped_count == 0

    pr = _FakePR()
    client = GitHubClient(ReviewConfig(github_token="t", repository="o/r"))
    post_inline_comments(
        client,
        cast(Any, pr),
        "cafebabe",
        build_result.payloads[:1],
        dry_run=False,
        debug=lambda level, message: debug_messages.append(f"{level}:{message}"),
    )
    assert pr._requester.calls[0][1].endswith("/comments")

    post_inline_comments(
        client,
        cast(Any, pr),
        "cafebabe",
        build_result.payloads[1:],
        dry_run=True,
        debug=lambda level, message: debug_messages.append(f"{level}:{message}"),
    )
    assert any("DRY_RUN: would POST /comments" in message for message in debug_messages)


def test_github_client_helpers_cover_normalization_and_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _extract_review_threads_page(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {"id": "thread-1"},
                                {"id": 99},
                                {"id": "thread-2", "isResolved": True},
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            }
        }
    )
    assert page.threads == [
        ReviewThreadSnapshot(id="thread-1", is_resolved=False, comments=[]),
        ReviewThreadSnapshot(id="thread-2", is_resolved=True, comments=[]),
    ]
    assert page.has_next_page is True
    assert page.end_cursor == "cursor-1"

    normalized_comment = _normalize_comment(
        {
            "id": "comment-1",
            "body": "text",
            "path": "sample.py",
            "line": 4,
            "originalLine": 3,
            "author": {"login": "octocat"},
        }
    )
    assert normalized_comment == ReviewThreadComment(
        id="comment-1",
        body="text",
        path="sample.py",
        line=4,
        original_line=3,
        author="octocat",
    )
    assert _normalize_comment({"id": "comment-2", "body": "text", "path": ""}) is None
    assert _normalize_comment({"id": "", "body": "text", "path": "sample.py"}) is None

    pr = _FakePR()
    client = GitHubClient(ReviewConfig(github_token="t", repository="o/r"))
    client.reply_to_review_comment(cast(Any, pr), 12, "reply body")
    assert pr._requester.calls[0][1].endswith("/comments/12/replies")
    client.resolve_review_thread(cast(Any, pr), "thread-123")
    assert pr._requester.calls[1][0] == "GRAPHQL"

    def _boom_request(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_post_pr_resource", _boom_request)
    with pytest.raises(Exception, match="failed replying to review comment 1"):
        client.reply_to_review_comment(cast(Any, _FakePR()), 1, "text")

    class _BrokenRequester(_FakeRequester):
        def graphql_query(
            self, query: str, variables: dict[str, Any]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            raise RuntimeError("boom")

    broken_pr = _FakePR()
    broken_pr._requester = _BrokenRequester()
    with pytest.raises(Exception, match="failed resolving review thread thread-1"):
        client.resolve_review_thread(cast(Any, broken_pr), "thread-1")

    class _BrokenIssue(_FakeIssue):
        def create_comment(self, text: str) -> None:  # noqa: ARG002
            raise RuntimeError("boom")

    class _IssueFailPR(_FakePR):
        def as_issue(self) -> _BrokenIssue:
            return _BrokenIssue()

    with pytest.raises(Exception, match="failed posting issue comment on PR #1"):
        client.post_issue_comment(cast(Any, _IssueFailPR()), "text")
    with pytest.raises(Exception, match="failed to post inline comment on PR #1"):
        client.post_inline_comment(
            cast(Any, _FakePR()),
            InlineCommentPayload(body="text", path="sample.py", line=1),
            head_sha="deadbeef",
        )
    assert CommentContext.from_mapping(
        {"id": "7", "event_name": "issue_comment"}
    ) == CommentContext(
        id=7,
        event_name="issue_comment",
        author="",
        body="",
    )


def test_github_client_wraps_repo_and_pr_load_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GitHubClient(ReviewConfig(github_token="t", repository="o/r"))

    class _BrokenGitHub:
        def get_repo(self, name: str) -> object:  # noqa: ARG002
            raise RuntimeError("boom")

    client._gh = cast(Any, _BrokenGitHub())
    with pytest.raises(Exception, match="failed to load repository o/r: boom"):
        client.get_repo()

    class _BrokenRepo:
        def get_pull(self, pr_number: int) -> object:  # noqa: ARG002
            raise RuntimeError("no pr")

    monkeypatch.setattr(client, "get_repo", lambda: cast(Any, _BrokenRepo()))
    with pytest.raises(Exception, match="failed to load pull request o/r#3: no pr"):
        client.get_pr(3)


def test_main_helpers_cover_commands_and_event_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert extract_edit_command("/codex fix this") == "fix this"
    assert extract_edit_command("/codex: fix this") == "fix this"
    assert extract_edit_command("/codex") is None
    assert extract_edit_command("/codex:") is None
    assert extract_edit_command("/codexify fix this") is None
    assert extract_edit_command("not a command") is None

    good_event = tmp_path / "event.json"
    good_event.write_text(json.dumps({"pull_request": {"number": 1}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(good_event))
    assert load_github_event() == {"pull_request": {"number": 1}}

    bad_event = tmp_path / "bad.json"
    bad_event.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(bad_event))
    with pytest.raises(Exception, match="expected object"):
        load_github_event()


def test_review_action_and_workflow_use_expected_resume_guard_and_model() -> None:
    action_yaml = Path("action.yml").read_text(encoding="utf-8")
    workflow_yaml = Path(".github/workflows/codex-review.yml").read_text(encoding="utf-8")

    assert (
        "steps.review_codex_cache.outputs.cache-hit == 'true' && "
        "steps.review_resume_state.outputs.restore_key == "
        "steps.review_resume_state.outputs.current_cache_key"
    ) in action_yaml
    assert "model: gpt-5.4" in workflow_yaml
    assert "model: gpt-5.1-codex-max" not in workflow_yaml


def test_edit_workflow_helpers_cover_reply_formatting_and_context_normalization() -> None:
    truncated = _format_edit_reply("x" * 3605, pushed=False, dry_run=False, changed=True)
    assert "not pushed" in truncated
    assert "… (truncated)" in truncated

    assert CommentContext.from_mapping(None) is None
    assert CommentContext.from_mapping(
        {"id": "9", "event_name": "issue_comment"}
    ) == CommentContext(
        id=9,
        event_name="issue_comment",
        author="",
        body="",
    )
