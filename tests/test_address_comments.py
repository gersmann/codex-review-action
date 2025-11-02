from __future__ import annotations

from pathlib import Path

from cli.config import ReviewConfig
from cli.edit_processor import EditProcessor


def _make_ep() -> EditProcessor:
    cfg = ReviewConfig(
        github_token="test",
        repository="o/r",
        pr_number=1,
        mode="act",
    )
    return EditProcessor(cfg)


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


def test_get_unresolved_threads_filters_resolved(monkeypatch) -> None:
    ep = _make_ep()

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
    res = ep._get_unresolved_threads(pr)  # type: ignore[arg-type]
    ids = {t.get("id") for t in res}
    assert ids == {"6", "7"}


def test_tip_copy_mentions_address_comments() -> None:
    text = Path("cli/review_processor.py").read_text(encoding="utf-8")
    assert '"/codex address comments"' in text


def test_process_edit_command_reports_fetch_errors(monkeypatch) -> None:
    ep = _make_ep()

    # Fake PR/repo
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

    class _Repo:
        pass

    pr = _PR()
    pr._iss = _Issue()  # type: ignore[attr-defined]

    # Wire EP to return our fake repo/pr
    def _fake_get_repo_and_pr(self, pr_number: int):  # noqa: ANN001
        return _Repo(), pr

    monkeypatch.setattr(EditProcessor, "_get_repo_and_pr", _fake_get_repo_and_pr)
    # Make thread fetch fail
    monkeypatch.setattr(
        EditProcessor,
        "_get_unresolved_threads",
        lambda self, pr: (_ for _ in ()).throw(RuntimeError("boom")),
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
