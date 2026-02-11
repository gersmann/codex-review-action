from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.config import ReviewConfig
from cli.git_ops import GitPushResult, GitWorktreeSnapshot
from cli.github_client import get_unresolved_threads
from cli.workflows.edit_workflow import EditWorkflow


def _make_ep() -> EditWorkflow:
    cfg = ReviewConfig(
        github_token="test",
        repository="o/r",
        pr_number=1,
        mode="act",
    )
    return EditWorkflow(cfg)


def test_intent_detection_variants() -> None:
    ep = _make_ep()
    should_match = [
        "/codex address comments in the PR",
        "/codex please fix the comments",
        "/codex resolve review threads",
    ]
    for s in should_match:
        assert ep._wants_fix_unresolved(s)

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
        assert not ep._wants_fix_unresolved(s)


def test_get_unresolved_threads_filters_resolved() -> None:
    class _Req:
        def requestJsonAndCheck(self, method: str, url: str):  # noqa: N802
            threads = [
                {"id": "1", "resolved": True, "comments": []},
                {"id": "2", "is_resolved": True, "comments": []},
                {"id": "3", "isResolved": True, "comments": []},
                {"id": "4", "state": "resolved", "comments": []},
                {"id": "5", "resolution": "completed", "comments": []},
                {"id": "6", "comments": [{"path": "a.py", "body": "x"}]},
                {"id": "7", "state": "active", "comments": [{"path": "b.py"}]},
            ]
            return ({}, threads)

    class _PR:
        url = "https://api.example/pr/1"
        _requester = _Req()

    pr = _PR()
    res = get_unresolved_threads(pr)
    ids = {t.get("id") for t in res}
    assert ids == {"6", "7"}


def test_tip_copy_mentions_address_comments() -> None:
    text = Path("cli/workflows/review_workflow.py").read_text(encoding="utf-8")
    assert '"/codex address comments"' in text


def test_process_edit_command_reports_fetch_errors() -> None:
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
            raise RuntimeError("boom")

        def safe_reply(self, current_pr: _PR, comment_ctx, text: str, debug):  # noqa: ARG002
            current_pr.as_issue().create_comment(text)

    ep = EditWorkflow(
        ReviewConfig(
            github_token="test",
            repository="o/r",
            pr_number=1,
            mode="act",
        ),
        github_client=_FakeGitHubClient(),
    )

    comment_ctx = {
        "id": 123,
        "event_name": "issue_comment",
        "author": "octocat",
        "body": "/codex address comments",
    }
    rc = ep.process_edit_command("/codex address comments", 1, comment_ctx)
    # exit code 1 and a reply posted
    assert rc == 1
    assert pr._iss.comments and "Failed to retrieve review threads:" in pr._iss.comments[0]  # type: ignore[attr-defined]

    # no allowed_files block in simplified prompt; no test required


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

        def safe_reply(self, current_pr, comment_ctx, text: str, debug):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute(self, prompt: str, config_overrides=None) -> str:  # noqa: ARG002
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

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
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
        github_client=_FakeGitHubClient(),
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

        def safe_reply(self, current_pr, comment_ctx, text: str, debug):  # noqa: ARG002
            return None

    class _FakeCodexClient:
        def execute(self, prompt: str, config_overrides=None) -> str:  # noqa: ARG002
            return "ok"

    before = GitWorktreeSnapshot(changed_paths=frozenset(), path_states={})
    after = GitWorktreeSnapshot(
        changed_paths=frozenset({"a.py", "b.py"}),
        path_states={"a.py": (True, "h1"), "b.py": (True, "h2")},
    )
    snapshots = iter([before, after])

    monkeypatch.setattr(workflow_mod, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(workflow_mod, "git_has_changes", lambda: True)
    monkeypatch.setattr(workflow_mod, "git_head_is_ahead", lambda branch: False)  # noqa: ARG005
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
        github_client=_FakeGitHubClient(),
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

        def safe_reply(self, current_pr, comment_ctx, text: str, debug):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute(self, prompt: str, config_overrides=None) -> str:  # noqa: ARG002
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

    def _capture_force_push(branch: str, expected_remote_sha: str | None) -> GitPushResult:
        force_push_calls.append((branch, expected_remote_sha))
        return GitPushResult(
            command=("git", "push", "origin"),
            returncode=0,
            stdout="",
            stderr="",
        )

    def _unexpected_normal_push(branch: str, debug):  # noqa: ARG001
        raise AssertionError("regular push should not be used for rewritten history")

    monkeypatch.setattr(workflow_mod, "git_push_force_with_lease", _capture_force_push)
    monkeypatch.setattr(workflow_mod, "git_push_head_to_branch", _unexpected_normal_push)

    fake_gh = _FakeGitHubClient()
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


def test_process_edit_command_fails_fast_for_active_rebase(monkeypatch) -> None:
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

        def safe_reply(self, current_pr, comment_ctx, text: str, debug):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute(self, prompt: str, config_overrides=None) -> str:  # noqa: ARG002
            return "ok"

    monkeypatch.setattr(
        workflow_mod,
        "git_worktree_snapshot",
        lambda: GitWorktreeSnapshot(changed_paths=frozenset(), path_states={}),
    )
    monkeypatch.setattr(workflow_mod, "git_current_head_sha", lambda: "head-before")
    monkeypatch.setattr(workflow_mod, "git_remote_head_sha", lambda branch: "remote-head")  # noqa: ARG005
    monkeypatch.setattr(workflow_mod, "git_rebase_in_progress", lambda: True)

    fake_gh = _FakeGitHubClient()
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

    rc = workflow.process_edit_command("/codex rebase onto main", 1, comment_ctx=None)

    assert rc == 2
    assert fake_gh.replies
    assert "active rebase state" in fake_gh.replies[0]


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

        def safe_reply(self, current_pr, comment_ctx, text: str, debug):  # noqa: ARG002
            self.replies.append(text)

    class _FakeCodexClient:
        def execute(self, prompt: str, config_overrides=None) -> str:  # noqa: ARG002
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
        lambda branch, expected_remote_sha: GitPushResult(  # noqa: ARG005
            command=(
                "git",
                "push",
                "origin",
                "HEAD:refs/heads/feature",
                "--force-with-lease",
            ),
            returncode=1,
            stdout="",
            stderr="stale info",
        ),
    )

    fake_gh = _FakeGitHubClient()
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
    assert fake_gh.replies
    assert "--force-with-lease" in fake_gh.replies[0]
    assert "stale info" in fake_gh.replies[0]
