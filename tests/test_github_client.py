from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from cli.clients.github_client import GitHubClient
from cli.core.config import ReviewConfig
from cli.core.github_types import ReviewRequestContext
from cli.core.models import AckComment


class _Requester:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def requestJsonAndCheck(  # noqa: N802
        self, verb: str, url: str, input: dict[str, Any] | None = None
    ) -> object:
        self.requests.append((verb, url, input))
        return ({"status": "201"}, {"id": 999})


@dataclass
class _CreatedIssueComment:
    id: int = 888


class _Issue:
    def __init__(self) -> None:
        self.comments: list[str] = []

    def create_comment(self, text: str) -> object:
        self.comments.append(text)
        return _CreatedIssueComment()


@dataclass
class _PR:
    number: int = 17
    url: str = "https://api.github.test/repos/owner/repo/pulls/17"

    def __post_init__(self) -> None:
        self._requester = _Requester()
        self.issue = _Issue()

    def as_issue(self) -> _Issue:
        return self.issue


def _client() -> GitHubClient:
    return GitHubClient(
        ReviewConfig(
            github_token="token",
            repository="owner/repo",
            pr_number=17,
            openai_api_key="test-key",
        )
    )


def test_acknowledges_issue_comment_with_reaction_and_pr_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    pr = _PR()
    monkeypatch.setattr(client, "get_pr", lambda pr_number: cast(Any, pr))

    ack = client.acknowledge_review_request(
        17,
        ReviewRequestContext(
            comment_id=123,
            commenter_login="octocat",
            event_name="issue_comment",
        ),
    )

    assert pr._requester.requests == [
        (
            "POST",
            "https://api.github.test/repos/owner/repo/issues/comments/123/reactions",
            {"content": "rocket"},
        )
    ]
    assert pr.issue.comments == ["@octocat, your Codex review has been queued."]
    assert ack == AckComment(comment_id=888, event_name="issue_comment")


def test_acknowledges_inline_comment_with_reaction_and_inline_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    pr = _PR()
    monkeypatch.setattr(client, "get_pr", lambda pr_number: cast(Any, pr))

    ack = client.acknowledge_review_request(
        17,
        ReviewRequestContext(
            comment_id=456,
            commenter_login="octocat",
            event_name="pull_request_review_comment",
        ),
    )

    assert pr._requester.requests == [
        (
            "POST",
            "https://api.github.test/repos/owner/repo/pulls/comments/456/reactions",
            {"content": "rocket"},
        ),
        (
            "POST",
            "https://api.github.test/repos/owner/repo/pulls/17/comments/456/replies",
            {"body": "@octocat, your Codex review has been queued."},
        ),
    ]
    assert ack == AckComment(comment_id=999, event_name="pull_request_review_comment")


def test_acknowledges_verify_request_with_verification_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    pr = _PR()
    monkeypatch.setattr(client, "get_pr", lambda pr_number: cast(Any, pr))

    ack = client.acknowledge_review_request(
        17,
        ReviewRequestContext(
            comment_id=123,
            commenter_login="octocat",
            event_name="issue_comment",
        ),
        kind="verification",
    )

    assert pr.issue.comments == ["@octocat, your Codex verification has been queued."]
    assert ack == AckComment(comment_id=888, event_name="issue_comment")


def test_delete_ack_comment_deletes_issue_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    pr = _PR()
    monkeypatch.setattr(client, "get_pr", lambda pr_number: cast(Any, pr))

    client.delete_ack_comment(17, AckComment(comment_id=888, event_name="issue_comment"))

    assert pr._requester.requests == [
        (
            "DELETE",
            "https://api.github.test/repos/owner/repo/issues/comments/888",
            None,
        )
    ]


def test_delete_ack_comment_deletes_review_comment_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    pr = _PR()
    monkeypatch.setattr(client, "get_pr", lambda pr_number: cast(Any, pr))

    client.delete_ack_comment(
        17,
        AckComment(comment_id=999, event_name="pull_request_review_comment"),
    )

    assert pr._requester.requests == [
        (
            "DELETE",
            "https://api.github.test/repos/owner/repo/pulls/comments/999",
            None,
        )
    ]
