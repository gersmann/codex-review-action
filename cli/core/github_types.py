from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UserLikeProtocol(Protocol):
    login: str


@runtime_checkable
class RepoOwnerLikeProtocol(Protocol):
    login: str


@runtime_checkable
class RepoLikeProtocol(Protocol):
    owner: RepoOwnerLikeProtocol
    name: str


@runtime_checkable
class HeadRefLikeProtocol(Protocol):
    ref: str
    sha: str
    label: str


@runtime_checkable
class BaseRefLikeProtocol(Protocol):
    ref: str
    sha: str
    label: str
    repo: RepoLikeProtocol


@runtime_checkable
class ChangedFileProtocol(Protocol):
    filename: str | None
    status: str
    patch: str | None
    previous_filename: str | None


@runtime_checkable
class IssueLikeProtocol(Protocol):
    def create_comment(self, text: str) -> object: ...


@runtime_checkable
class RepositoryLikeProtocol(Protocol):
    def get_pull(self, pr_number: int) -> PullRequestLikeProtocol: ...


@runtime_checkable
class IssueCommentLikeProtocol(Protocol):
    body: str | None
    created_at: object
    id: int
    user: UserLikeProtocol | None

    def delete(self) -> object: ...


@runtime_checkable
class ReviewCommentLikeProtocol(Protocol):
    body: str | None
    path: str | None
    line: int | None
    original_line: int | None
    in_reply_to_id: int | None
    diff_hunk: str | None
    commit_id: str | None
    created_at: object
    user: UserLikeProtocol | None


@runtime_checkable
class ReviewLikeProtocol(Protocol):
    body: str | None


@runtime_checkable
class RequesterLikeProtocol(Protocol):
    def requestJsonAndCheck(self, verb: str, url: str, input: dict[str, Any]) -> object: ...

    def graphql_query(
        self, query: str, variables: Mapping[str, object]
    ) -> tuple[object, object]: ...


@runtime_checkable
class StatusCodeErrorProtocol(Protocol):
    status: int


@runtime_checkable
class PullRequestLikeProtocol(Protocol):
    number: int
    title: str
    body: str | None
    html_url: str
    state: str
    url: str
    user: UserLikeProtocol | None
    head: HeadRefLikeProtocol | None
    base: BaseRefLikeProtocol | None
    _requester: RequesterLikeProtocol

    def get_files(self) -> Iterable[ChangedFileProtocol]: ...

    def get_issue_comments(self) -> Iterable[IssueCommentLikeProtocol]: ...

    def get_review_comments(self) -> Iterable[ReviewCommentLikeProtocol]: ...

    def get_reviews(self) -> Iterable[ReviewLikeProtocol]: ...

    def get_review_comment(self, comment_id: int) -> ReviewCommentLikeProtocol: ...

    def as_issue(self) -> IssueLikeProtocol: ...
