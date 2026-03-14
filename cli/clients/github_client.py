from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from github import Github

from ..core.config import ReviewConfig
from ..core.exceptions import GitHubAPIError
from ..core.github_types import (
    PullRequestLikeProtocol,
    RepositoryLikeProtocol,
    StatusCodeErrorProtocol,
)
from ..core.models import (
    InlineCommentPayload,
    UnresolvedReviewComment,
    UnresolvedReviewThread,
)


class GitHubClientProtocol(Protocol):
    """Interface for GitHub client used by workflows."""

    def get_pr(self, pr_number: int) -> PullRequestLikeProtocol: ...
    def get_unresolved_threads(
        self, pr: PullRequestLikeProtocol
    ) -> list[UnresolvedReviewThread]: ...
    def post_inline_comment(
        self,
        pr: PullRequestLikeProtocol,
        payload: InlineCommentPayload,
        *,
        head_sha: str,
    ) -> None: ...
    def reply_to_review_comment(
        self,
        pr: PullRequestLikeProtocol,
        comment_id: int,
        text: str,
    ) -> None: ...
    def post_issue_comment(self, pr: PullRequestLikeProtocol, text: str) -> None: ...


class GitHubClient:
    """Concrete GitHub client wrapper used by workflows."""

    def __init__(self, config: ReviewConfig) -> None:
        self._config = config
        self._gh: Github | None = None

    def _client(self) -> Github:
        if self._gh is None:
            self._gh = Github(login_or_token=self._config.github_token, per_page=100)
        return self._gh

    def get_repo(self) -> RepositoryLikeProtocol:
        repo_name = f"{self._config.owner}/{self._config.repo_name}"
        try:
            return cast(RepositoryLikeProtocol, self._client().get_repo(repo_name))
        except Exception as exc:
            raise _wrap_github_error(f"failed to load repository {repo_name}", exc) from exc

    def get_pr(self, pr_number: int) -> PullRequestLikeProtocol:
        try:
            return self.get_repo().get_pull(pr_number)
        except GitHubAPIError:
            raise
        except Exception as exc:
            target = f"{self._config.owner}/{self._config.repo_name}#{pr_number}"
            raise _wrap_github_error(f"failed to load pull request {target}", exc) from exc

    def get_unresolved_threads(self, pr: PullRequestLikeProtocol) -> list[UnresolvedReviewThread]:
        owner, repo_name, pr_number = _resolve_pr_identity(pr)
        threads: list[UnresolvedReviewThread] = []
        cursor: str | None = None

        while True:
            variables: dict[str, str | int | None] = {
                "owner": owner,
                "name": repo_name,
                "number": pr_number,
                "after": cursor,
            }
            try:
                _, raw = pr._requester.graphql_query(_REVIEW_THREADS_QUERY, variables)
            except Exception as exc:
                target = f"{owner}/{repo_name}#{pr_number}"
                raise _wrap_github_error(f"fetch error for reviewThreads on {target}", exc) from exc

            page = _extract_review_threads_page(raw)
            threads.extend(page.threads)

            if not page.has_next_page:
                return threads

            if not page.end_cursor:
                raise GitHubAPIError("missing endCursor for paginated reviewThreads response")
            cursor = page.end_cursor

    def post_inline_comment(
        self,
        pr: PullRequestLikeProtocol,
        payload: InlineCommentPayload,
        *,
        head_sha: str,
    ) -> None:
        try:
            self._post_pr_resource(pr, "comments", payload.to_request_payload(head_sha))
        except Exception as exc:
            raise _wrap_github_error(
                f"failed to post inline comment on PR #{pr.number}",
                exc,
            ) from exc

    def reply_to_review_comment(
        self,
        pr: PullRequestLikeProtocol,
        comment_id: int,
        text: str,
    ) -> None:
        try:
            self._post_pr_resource(
                pr,
                f"comments/{comment_id}/replies",
                {"body": text},
            )
        except Exception as exc:
            raise _wrap_github_error(
                f"failed replying to review comment {comment_id} on PR #{pr.number}",
                exc,
            ) from exc

    def post_issue_comment(
        self,
        pr: PullRequestLikeProtocol,
        text: str,
    ) -> None:
        try:
            pr.as_issue().create_comment(text)
        except Exception as exc:
            raise _wrap_github_error(
                f"failed posting issue comment on PR #{pr.number}",
                exc,
            ) from exc

    def _post_pr_resource(
        self,
        pr: PullRequestLikeProtocol,
        url_suffix: str,
        body: dict[str, Any],
    ) -> object:
        url = f"{pr.url}/{url_suffix}"
        return pr._requester.requestJsonAndCheck("POST", url, input=body)


_REVIEW_THREADS_QUERY = """
query ReviewThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes {
              id
              body
              path
              line
              originalLine
              author {
                login
              }
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ReviewThreadsPage:
    threads: list[UnresolvedReviewThread]
    has_next_page: bool
    end_cursor: str | None


def _resolve_pr_identity(pr: PullRequestLikeProtocol) -> tuple[str, str, int]:
    try:
        base = pr.base
        if base is None:
            raise GitHubAPIError("missing owner/repo identity for GraphQL reviewThreads query")
        owner_value = base.repo.owner.login
        repo_name_value = base.repo.name
        pr_number_value = pr.number
    except Exception as exc:
        raise GitHubAPIError("missing PR identity for GraphQL reviewThreads query") from exc

    owner = owner_value if isinstance(owner_value, str) else ""
    repo_name = repo_name_value if isinstance(repo_name_value, str) else ""
    if not isinstance(pr_number_value, int):
        raise GitHubAPIError("invalid PR number for GraphQL reviewThreads query")
    if not owner or not repo_name:
        raise GitHubAPIError("missing owner/repo identity for GraphQL reviewThreads query")
    return owner, repo_name, pr_number_value


def _extract_review_threads_page(raw: object) -> ReviewThreadsPage:
    root = _require_mapping(raw, "unexpected GraphQL response type for reviewThreads")
    data = _require_mapping_field(root, "data", "GraphQL response missing data object")
    repository = _require_mapping_field(
        data,
        "repository",
        "GraphQL response missing repository object",
    )
    pull_request = _require_mapping_field(
        repository,
        "pullRequest",
        "GraphQL response missing pullRequest object",
    )
    review_threads = _require_mapping_field(
        pull_request,
        "reviewThreads",
        "GraphQL response missing reviewThreads object",
    )

    nodes_value = _require_list_field(
        review_threads,
        "nodes",
        "GraphQL reviewThreads.nodes must be a list",
    )
    page_info = _require_mapping_field(
        review_threads,
        "pageInfo",
        "GraphQL reviewThreads.pageInfo must be an object",
    )
    has_next_page_value = _require_bool_field(
        page_info,
        "hasNextPage",
        "GraphQL reviewThreads.pageInfo.hasNextPage must be a bool",
    )
    end_cursor_value = page_info.get("endCursor")
    end_cursor = end_cursor_value if isinstance(end_cursor_value, str) else None
    threads = _normalize_threads(nodes_value)

    return ReviewThreadsPage(
        threads=threads,
        has_next_page=has_next_page_value,
        end_cursor=end_cursor,
    )


def _require_mapping(value: object, error_message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubAPIError(error_message)
    return cast(Mapping[str, object], value)


def _require_mapping_field(
    container: Mapping[str, object],
    field: str,
    error_message: str,
) -> Mapping[str, object]:
    return _require_mapping(container.get(field), error_message)


def _require_list_field(
    container: Mapping[str, object],
    field: str,
    error_message: str,
) -> list[object]:
    value = container.get(field)
    if not isinstance(value, list):
        raise GitHubAPIError(error_message)
    return value


def _require_bool_field(
    container: Mapping[str, object],
    field: str,
    error_message: str,
) -> bool:
    value = container.get(field)
    if not isinstance(value, bool):
        raise GitHubAPIError(error_message)
    return value


def _normalize_threads(nodes: list[object]) -> list[UnresolvedReviewThread]:
    threads: list[UnresolvedReviewThread] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        normalized_thread = _normalize_thread(cast(Mapping[str, object], node))
        if normalized_thread is None:
            continue
        threads.append(normalized_thread)
    return threads


def _normalize_thread(thread: Mapping[str, object]) -> UnresolvedReviewThread | None:
    if thread.get("isResolved") is True:
        return None
    thread_id_value = thread.get("id")
    if not isinstance(thread_id_value, str) or not thread_id_value:
        return None
    thread_id = thread_id_value
    comments: list[UnresolvedReviewComment] = []

    comments_connection = thread.get("comments")
    if isinstance(comments_connection, Mapping):
        nodes_value = comments_connection.get("nodes")
        if isinstance(nodes_value, list):
            for comment_value in nodes_value:
                if not isinstance(comment_value, Mapping):
                    continue
                normalized_comment = _normalize_comment(comment_value)
                if normalized_comment is None:
                    continue
                comments.append(normalized_comment)

    return UnresolvedReviewThread(id=thread_id, comments=comments)


def _normalize_comment(comment: Mapping[str, object]) -> UnresolvedReviewComment | None:
    comment_id_value = comment.get("id")
    if not isinstance(comment_id_value, str) or not comment_id_value:
        return None
    comment_id = comment_id_value

    author_value = comment.get("author")
    login = ""
    if isinstance(author_value, Mapping):
        login_value = author_value.get("login")
        if isinstance(login_value, str):
            login = login_value

    body_value = comment.get("body")
    path_value = comment.get("path")
    line_value = comment.get("line")
    line = line_value if isinstance(line_value, int) else None
    original_line_value = comment.get("originalLine")
    original_line = original_line_value if isinstance(original_line_value, int) else None
    if not isinstance(path_value, str) or not path_value:
        return None

    return UnresolvedReviewComment(
        id=comment_id,
        body=body_value if isinstance(body_value, str) else "",
        path=path_value,
        line=line,
        original_line=original_line,
        author=login,
    )


def _wrap_github_error(message: str, exc: Exception) -> GitHubAPIError:
    status_code = exc.status if isinstance(exc, StatusCodeErrorProtocol) else None
    detail = str(exc).strip()
    suffix = f": {detail}" if detail else ""
    return GitHubAPIError(f"{message}{suffix}", status_code=status_code)
