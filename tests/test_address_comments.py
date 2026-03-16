from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

from cli.clients.git_ops import GitWorktreeSnapshot
from cli.clients.github_client import GitHubClient
from cli.core.config import ReviewConfig
from cli.core.exceptions import GitHubAPIError
from cli.core.models import CommentContext, ReviewRunResult, UnresolvedReviewComment
from cli.review.posting import ReviewPostingOutcome
from cli.workflows.edit_prompt import CommentContextRenderResult
from cli.workflows.edit_workflow import EditWorkflow, _wants_fix_unresolved
from cli.workflows.review_workflow import SUMMARY_TIP, ReviewSummary, _build_review_summary


def _make_ep() -> EditWorkflow:
    cfg = ReviewConfig(
        github_token="test",
        repository="o/r",
        pr_number=1,
        mode="act",
    )
    return EditWorkflow(cfg)


def test_intent_detection_variants() -> None:
    should_match = [
        "/codex address comments in the PR",
        "/codex please fix the comments",
        "/codex resolve review threads",
    ]
    for s in should_match:
        assert _wants_fix_unresolved(s)

    should_not = [
        "/codex do not address comments yet",
        "/codex don't fix comments",
        "/codex address performance issues",
        "/codex fix docs",
        "/codex handle comments",  # no longer matched by simplified verbs
        "/codex deal with feedback",  # no longer matched by simplified verbs
        "/codex clean up comments",  # no longer matched by simplified verbs
    ]
    for s in should_not:
        assert not _wants_fix_unresolved(s)


def test_get_unresolved_threads_filters_resolved() -> None:
    class _Req:
        def graphql_query(self, query: str, variables: dict[str, object]):  # noqa: ARG002
            assert variables["owner"] == "o"
            assert variables["name"] == "r"
            assert variables["number"] == 1
            return (
                {},
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "isResolved": True,
                                            "comments": {"nodes": []},
                                        },
                                        {
                                            "id": "thread-2",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "id": "comment-1",
                                                        "body": "please fix",
                                                        "path": "a.py",
                                                        "line": 12,
                                                        "originalLine": 10,
                                                        "author": {"login": "alice"},
                                                    }
                                                ]
                                            },
                                        },
                                        {
                                            "id": "thread-3",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "id": "comment-2",
                                                        "body": "nit",
                                                        "path": "b.py",
                                                        "line": 7,
                                                        "originalLine": 7,
                                                        "author": {"login": "bob"},
                                                    }
                                                ]
                                            },
                                        },
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                },
            )

    class _Owner:
        login = "o"

    class _Repo:
        owner = _Owner()
        name = "r"

    class _Base:
        repo = _Repo()

    class _PR:
        number = 1
        base = _Base()
        _requester = _Req()

    pr = _PR()
    client = GitHubClient(ReviewConfig(github_token="test", repository="o/r"))
    res = client.get_unresolved_threads(cast(Any, pr))
    ids = {t.id for t in res}
    assert ids == {"thread-2", "thread-3"}

    comments = [comment for thread in res for comment in thread.comments]
    assert comments == [
        UnresolvedReviewComment(
            id="comment-1",
            body="please fix",
            path="a.py",
            line=12,
            original_line=10,
            author="alice",
        ),
        UnresolvedReviewComment(
            id="comment-2",
            body="nit",
            path="b.py",
            line=7,
            original_line=7,
            author="bob",
        ),
    ]


def test_get_unresolved_threads_paginates_graphql_results() -> None:
    class _Req:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def graphql_query(self, query: str, variables: dict[str, object]):  # noqa: ARG002
            self.calls.append(dict(variables))
            after = variables.get("after")
            if after is None:
                return (
                    {},
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "thread-1",
                                                "isResolved": False,
                                                "comments": {"nodes": []},
                                            }
                                        ],
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "cursor-1",
                                        },
                                    }
                                }
                            }
                        }
                    },
                )

            assert after == "cursor-1"
            return (
                {},
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-2",
                                            "isResolved": True,
                                            "comments": {"nodes": []},
                                        },
                                        {
                                            "id": "thread-3",
                                            "isResolved": False,
                                            "comments": {"nodes": []},
                                        },
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                },
            )

    class _Owner:
        login = "o"

    class _Repo:
        owner = _Owner()
        name = "r"

    class _Base:
        repo = _Repo()

    class _PR:
        number = 42
        base = _Base()
        _requester = _Req()

    pr = _PR()
    client = GitHubClient(ReviewConfig(github_token="test", repository="o/r"))
    result = client.get_unresolved_threads(cast(Any, pr))
    assert [thread.id for thread in result] == ["thread-1", "thread-3"]
    assert pr._requester.calls == [
        {"owner": "o", "name": "r", "number": 42, "after": None},
        {"owner": "o", "name": "r", "number": 42, "after": "cursor-1"},
    ]


def test_review_summary_mentions_address_comments_tip() -> None:
    summary = _build_review_summary(
        ReviewRunResult(
            overall_correctness="patch is correct",
            overall_explanation="",
            overall_confidence_score=None,
            findings=[],
            carried_forward=[],
        ),
        ReviewSummary(
            overall_correctness="patch is correct",
            current_findings_count=0,
            carried_forward_count=0,
            active_findings_count=0,
        ),
        ReviewPostingOutcome.empty(0),
    )

    assert SUMMARY_TIP in summary
    assert "/codex address comments" in summary


def test_process_edit_command_fails_on_thread_fetch_errors(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    # Fake PR
    class _Issue:
        def __init__(self) -> None:
            self.comments: list[str] = []

        def create_comment(self, text: str) -> None:  # noqa: D401
            self.comments.append(text)

    class _PR:
        url = "https://api.example/repos/o/r/pulls/1"

        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

        def as_issue(self) -> _Issue:
            return self._iss  # type: ignore[attr-defined]

    pr = _PR()
    pr._iss = _Issue()  # type: ignore[attr-defined]

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return pr

        def get_unresolved_threads(self, current_pr: _PR):  # noqa: ARG002
            raise GitHubAPIError("boom")

        def reply_to_review_comment(self, current_pr: _PR, comment_id: int, text: str):  # noqa: ARG002
            current_pr.as_issue().create_comment(text)

        def post_issue_comment(self, current_pr: _PR, text: str):  # noqa: ARG002
            current_pr.as_issue().create_comment(text)

    class _FakeCodexClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            self.prompts.append(prompt)
            return "ok"

    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote")  # noqa: ARG005
    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005

    fake_codex = _FakeCodexClient()

    ep = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, fake_codex),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    comment_ctx = CommentContext(
        id=123,
        event_name="issue_comment",
        author="octocat",
        body="/codex address comments",
    )
    rc = ep.process_edit_command("/codex address comments", 1, comment_ctx)
    assert rc == 2
    assert pr._iss.comments and "Failed to retrieve review threads;" in pr._iss.comments[0]  # type: ignore[attr-defined]
    assert fake_codex.prompts == []


def test_process_edit_command_surfaces_comment_context_warnings(monkeypatch, capsys) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.replies: list[str] = []

        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            self.replies.append(text)

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    monkeypatch.setattr(
        workflow_mod,
        "build_comment_context_block",
        lambda *args, **kwargs: CommentContextRenderResult(
            warning="Continuing with degraded comment context for review comment 123: missing file",
            status="degraded",
        ),
    )
    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005

    fake_gh = _FakeGitHubClient()
    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, fake_gh),
    )

    comment_ctx = CommentContext(
        id=123,
        event_name="issue_comment",
        author="octocat",
        body="/codex fix docs",
    )
    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx)

    err = capsys.readouterr().err
    assert rc == 0
    assert "Continuing with degraded comment context for review comment 123: missing file" in err
    assert fake_gh.replies
    assert (
        "Continuing with degraded comment context for review comment 123: missing file"
        in fake_gh.replies[0]
    )


def test_process_edit_command_prints_reply_failures(monkeypatch, capsys) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            raise GitHubAPIError("reply boom")

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            raise GitHubAPIError("reply boom")

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    comment_ctx = CommentContext(
        id=123,
        event_name="issue_comment",
        author="octocat",
        body="/codex fix docs",
    )
    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx)

    err = capsys.readouterr().err
    assert rc == 0
    assert "Failed to reply to comment 123: reply boom" in err
    assert "GitHub reply delivery failed after a locally successful edit workflow result." in err


def test_process_edit_command_skips_commit_when_no_agent_scoped_changes(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    before = GitWorktreeSnapshot(
        changed_paths=frozenset({"preexisting.py"}),
        path_states={"preexisting.py": (True, "hash-a")},
    )
    after = GitWorktreeSnapshot(
        changed_paths=frozenset({"preexisting.py"}),
        path_states={"preexisting.py": (True, "hash-a")},
    )
    snapshots = iter([before, after])
    head_shas = iter(["head-before", "head-after"])

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: next(head_shas))
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: True)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005

    def _unexpected_commit(message: str, paths):  # noqa: ARG001
        raise AssertionError("commit should not be called for non-agent-scoped changes")

    monkeypatch.setattr(workflow_mod, "git_commit_paths", _unexpected_commit)

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx=None)
    assert rc == 0


def test_process_edit_command_commits_only_agent_scoped_paths(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    before = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    after = GitWorktreeSnapshot(
        changed_paths=frozenset({"a.py", "b.py"}),
        path_states={"a.py": (True, "h1"), "b.py": (True, "h2")},
    )
    snapshots = iter([before, after])
    head_shas = iter(["head-before", "head-after"])

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: next(head_shas))
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: True)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_is_ancestor", lambda older, newer: True)  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_setup_identity", lambda: None)

    committed: list[list[str]] = []
    pushed: list[str] = []

    def _capture_commit(message: str, paths):  # noqa: ARG001
        committed.append(list(paths))
        return True

    monkeypatch.setattr(workflow_mod, "git_commit_paths", _capture_commit)
    monkeypatch.setattr(
        workflow_mod,
        "git_push_head_to_branch",
        lambda branch, debug: pushed.append(branch),  # noqa: ARG005
    )
    monkeypatch.setattr(workflow_mod, "git_push", lambda: None)

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx=None)

    assert rc == 0
    assert committed == [["a.py", "b.py"]]
    assert pushed == ["feature"]


def test_process_edit_command_uses_force_with_lease_for_rewritten_history(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.replies: list[str] = []

        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            self.replies.append(text)

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    before = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    after = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    snapshots = iter([before, after])
    head_shas = iter(["head-before", "head-after"])

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: next(head_shas))
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: True)  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_is_ancestor", lambda older, newer: False)  # noqa: ARG005

    force_push_calls: list[tuple[str, str | None]] = []

    def _capture_force_push(branch: str, expected_remote_sha: str | None) -> None:
        force_push_calls.append((branch, expected_remote_sha))
        return None

    def _unexpected_normal_push(branch: str, debug):  # noqa: ARG001
        raise AssertionError("regular push should not be used for rewritten history")

    monkeypatch.setattr(workflow_mod, "git_push_force_with_lease", _capture_force_push)
    monkeypatch.setattr(workflow_mod, "git_push_head_to_branch", _unexpected_normal_push)

    fake_gh = cast(Any, _FakeGitHubClient())
    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=fake_gh,
    )

    rc = workflow.process_edit_command("/codex rebase branch", 1, comment_ctx=None)

    assert rc == 0
    assert force_push_calls == [("feature", "remote-head")]


def test_process_edit_command_fails_fast_when_rebase_is_active(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.replies: list[str] = []

        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            self.replies.append(text)

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def __init__(self) -> None:
            self.calls = 0

        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            self.calls += 1
            return "ok"

    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head-before")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: True)

    fake_gh = cast(Any, _FakeGitHubClient())
    fake_codex = _FakeCodexClient()
    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, fake_codex),
        github_client=fake_gh,
    )

    rc = workflow.process_edit_command("/codex rebase onto main", 1, comment_ctx=None)

    assert rc == 2
    assert fake_gh.replies == []
    assert fake_codex.calls == 0


def test_process_edit_command_reports_force_with_lease_failures(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.replies: list[str] = []

        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            self.replies.append(text)

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    before = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    after = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    snapshots = iter([before, after])
    head_shas = iter(["head-before", "head-after"])

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: next(head_shas))
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: True)  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_is_ancestor", lambda older, newer: False)  # noqa: ARG005

    monkeypatch.setattr(
        workflow_mod,
        "git_push_force_with_lease",
        lambda branch, expected_remote_sha: (_ for _ in ()).throw(  # noqa: ARG005
            subprocess.CalledProcessError(
                1,
                [
                    "git",
                    "push",
                    "origin",
                    "HEAD:refs/heads/feature",
                    "--force-with-lease",
                ],
                "",
                "stale info",
            )
        ),
    )

    fake_gh = cast(Any, _FakeGitHubClient())
    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=fake_gh,
    )

    rc = workflow.process_edit_command("/codex rebase and push", 1, comment_ctx=None)

    assert rc == 2
    assert fake_gh.replies == []


def test_edit_workflow_debug2_does_not_dump_full_prompt(
    monkeypatch,
    capsys,
) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    monkeypatch.setattr(workflow_mod, "build_edit_prompt", lambda *args: "SECRET PROMPT BLOCK")
    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head-before")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
            debug_level=2,
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx=None)

    err = capsys.readouterr().err
    assert rc == 0
    assert "Edit prompt context" in err
    assert "SECRET PROMPT BLOCK" not in err


def test_process_edit_command_fails_when_ahead_probe_errors(monkeypatch) -> None:
    import cli.workflows.edit_workflow as workflow_mod

    class _PR:
        class _H:
            ref = "feature"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.replies: list[str] = []

        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _PR()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            self.replies.append(text)

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "ok"

    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head-before")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: False)
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: False)

    def _boom_ahead(branch: str | None) -> bool:  # noqa: ARG001
        raise subprocess.CalledProcessError(128, ["git", "rev-list"], "", "network down")

    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", _boom_ahead)

    fake_gh = cast(Any, _FakeGitHubClient())
    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=fake_gh,
    )

    rc = workflow.process_edit_command("/codex fix docs", 1, comment_ctx=None)

    assert rc == 2
    assert fake_gh.replies == []


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        command = " ".join(["git", *args])
        raise AssertionError(
            f"git command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def _setup_feature_branch_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    worktree = tmp_path / "worktree"

    _run_git(tmp_path, "init", "--bare", str(origin))
    _run_git(tmp_path, "clone", str(origin), str(worktree))

    _run_git(worktree, "config", "user.email", "integration@example.test")
    _run_git(worktree, "config", "user.name", "Integration Tester")
    _run_git(worktree, "checkout", "-b", "main")

    tracked_file = worktree / "app.txt"
    tracked_file.write_text("base\n", encoding="utf-8")
    _run_git(worktree, "add", "app.txt")
    _run_git(worktree, "commit", "-m", "initial")
    _run_git(worktree, "push", "-u", "origin", "main")

    _run_git(worktree, "checkout", "-b", "feature/integration")
    tracked_file.write_text("base\nfeature\n", encoding="utf-8")
    _run_git(worktree, "add", "app.txt")
    _run_git(worktree, "commit", "-m", "feature")
    _run_git(worktree, "push", "-u", "origin", "feature/integration")

    remote_feature_sha = _run_git(worktree, "rev-parse", "origin/feature/integration")
    return worktree, remote_feature_sha


def _make_integration_pr() -> Any:
    class _PR:
        class _H:
            ref = "feature/integration"

        class _B:
            ref = "main"

        head = _H()
        base = _B()

    return _PR()


def test_process_edit_command_git_integration_commits_and_pushes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worktree, remote_before = _setup_feature_branch_repo(tmp_path)
    monkeypatch.chdir(worktree)

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _make_integration_pr()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            target = worktree / "app.txt"
            target.write_text(
                target.read_text(encoding="utf-8") + "codex-change\n", encoding="utf-8"
            )
            return "applied"

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex apply integration change", 1, comment_ctx=None)

    local_head = _run_git(worktree, "rev-parse", "HEAD")
    remote_after = _run_git(worktree, "rev-parse", "origin/feature/integration")
    assert rc == 0
    assert local_head == remote_after
    assert remote_after != remote_before


def test_process_edit_command_git_integration_noop_preserves_branch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worktree, remote_before = _setup_feature_branch_repo(tmp_path)
    monkeypatch.chdir(worktree)

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _make_integration_pr()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            return "no changes"

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex noop", 1, comment_ctx=None)

    local_head = _run_git(worktree, "rev-parse", "HEAD")
    remote_after = _run_git(worktree, "rev-parse", "origin/feature/integration")
    assert rc == 0
    assert local_head == remote_before
    assert remote_after == remote_before


def test_process_edit_command_git_integration_rewritten_history_uses_force_push(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worktree, remote_before = _setup_feature_branch_repo(tmp_path)
    monkeypatch.chdir(worktree)

    class _FakeGitHubClient:
        def get_pr(self, pr_number: int):  # noqa: ARG002
            return _make_integration_pr()

        def get_unresolved_threads(self, current_pr):  # noqa: ARG002
            return []

        def reply_to_review_comment(self, current_pr, comment_id: int, text: str):  # noqa: ARG002
            return None

        def post_issue_comment(self, current_pr, text: str):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute_text(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
            _run_git(
                worktree,
                "commit",
                "--amend",
                "--allow-empty",
                "-m",
                "feature rewritten by codex",
            )
            return "rewrote history"

    workflow = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        codex_client=cast(Any, _FakeCodexClient()),
        github_client=cast(Any, _FakeGitHubClient()),
    )

    rc = workflow.process_edit_command("/codex rewrite", 1, comment_ctx=None)

    local_head = _run_git(worktree, "rev-parse", "HEAD")
    remote_after = _run_git(worktree, "rev-parse", "origin/feature/integration")
    ancestor_check = subprocess.run(
        ["git", "merge-base", "--is-ancestor", remote_before, local_head],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rc == 0
    assert local_head == remote_after
    assert local_head != remote_before
    assert ancestor_check.returncode == 1
