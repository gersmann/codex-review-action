from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from github import Github

from .config import ReviewConfig
from .models import CommentContext


@runtime_checkable
class GitHubClientProtocol(Protocol):
    """Interface for GitHub client used by workflows."""

    def get_pr(self, pr_number: int) -> Any: ...
    def get_review_threads(self, pr: Any) -> list[dict[str, Any]]: ...
    def get_unresolved_threads(self, pr: Any) -> list[dict[str, Any]]: ...
    def safe_reply(
        self,
        pr: Any,
        comment_ctx: Mapping[str, Any] | CommentContext | None,
        text: str,
        debug: Callable[[int, str], None],
    ) -> None: ...


def post_pr_resource(pr: Any, url_suffix: str, body: dict[str, Any]) -> Any:
    """POST to a PR sub-resource via PyGithub's internal requester."""
    url = f"{pr.url}/{url_suffix}"
    return pr._requester.requestJsonAndCheck("POST", url, input=body)


class GitHubClient:
    """Concrete GitHub client wrapper used by workflows."""

    def __init__(self, config: ReviewConfig) -> None:
        self._config = config
        self._gh: Github | None = None

    def _client(self) -> Github:
        if self._gh is None:
            self._gh = Github(login_or_token=self._config.github_token, per_page=100)
        return self._gh

    def get_repo(self) -> Any:
        return self._client().get_repo(f"{self._config.owner}/{self._config.repo_name}")

    def get_pr(self, pr_number: int) -> Any:
        return self.get_repo().get_pull(pr_number)

    def get_review_threads(self, pr: Any) -> list[dict[str, Any]]:
        return get_review_threads(pr)

    def get_unresolved_threads(self, pr: Any) -> list[dict[str, Any]]:
        return get_unresolved_threads(pr)

    def safe_reply(
        self,
        pr: Any,
        comment_ctx: Mapping[str, Any] | CommentContext | None,
        text: str,
        debug: Callable[[int, str], None],
    ) -> None:
        _safe_reply(pr, comment_ctx, text, debug)


def get_review_threads(pr: Any) -> list[dict[str, Any]]:
    """Fetch all review threads for a PR via GraphQL."""
    owner, repo_name, pr_number = _resolve_pr_identity(pr)
    threads: list[dict[str, Any]] = []
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
            raise RuntimeError(f"fetch error for reviewThreads on {target}: {exc}") from exc

        page_nodes, has_next_page, end_cursor = _extract_review_threads_page(raw)
        for node in page_nodes:
            threads.append(_normalize_thread(node))

        if not has_next_page:
            break

        if not end_cursor:
            raise RuntimeError("missing endCursor for paginated reviewThreads response")
        cursor = end_cursor

    return threads


def get_unresolved_threads(pr: Any) -> list[dict[str, Any]]:
    """Fetch unresolved review threads for a PR via GraphQL."""
    return [thread for thread in get_review_threads(pr) if not bool(thread.get("is_resolved"))]


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


def _resolve_pr_identity(pr: Any) -> tuple[str, str, int]:
    try:
        owner_value = pr.base.repo.owner.login
        repo_name_value = pr.base.repo.name
        pr_number_value = pr.number
    except Exception as exc:
        raise RuntimeError("missing PR identity for GraphQL reviewThreads query") from exc

    owner = owner_value if isinstance(owner_value, str) else ""
    repo_name = repo_name_value if isinstance(repo_name_value, str) else ""
    if not isinstance(pr_number_value, int):
        raise RuntimeError("invalid PR number for GraphQL reviewThreads query")
    if not owner or not repo_name:
        raise RuntimeError("missing owner/repo identity for GraphQL reviewThreads query")
    return owner, repo_name, pr_number_value


def _extract_review_threads_page(raw: Any) -> tuple[list[dict[str, Any]], bool, str | None]:
    if not isinstance(raw, dict):
        raise RuntimeError("unexpected GraphQL response type for reviewThreads")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL response missing data object")

    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise RuntimeError("GraphQL response missing repository object")

    pull_request = repository.get("pullRequest")
    if not isinstance(pull_request, dict):
        raise RuntimeError("GraphQL response missing pullRequest object")

    review_threads = pull_request.get("reviewThreads")
    if not isinstance(review_threads, dict):
        raise RuntimeError("GraphQL response missing reviewThreads object")

    nodes_value = review_threads.get("nodes")
    if not isinstance(nodes_value, list):
        raise RuntimeError("GraphQL reviewThreads.nodes must be a list")

    nodes: list[dict[str, Any]] = [node for node in nodes_value if isinstance(node, dict)]
    page_info = review_threads.get("pageInfo")
    if not isinstance(page_info, dict):
        raise RuntimeError("GraphQL reviewThreads.pageInfo must be an object")

    has_next_page_value = page_info.get("hasNextPage")
    if not isinstance(has_next_page_value, bool):
        raise RuntimeError("GraphQL reviewThreads.pageInfo.hasNextPage must be a bool")

    end_cursor_value = page_info.get("endCursor")
    end_cursor = end_cursor_value if isinstance(end_cursor_value, str) else None
    return nodes, has_next_page_value, end_cursor


def _is_thread_resolved(thread: dict[str, Any]) -> bool:
    return thread.get("isResolved") is True


def _normalize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    thread_id_value = thread.get("id")
    thread_id = thread_id_value if isinstance(thread_id_value, str) else ""
    comments: list[dict[str, Any]] = []

    comments_connection = thread.get("comments")
    if isinstance(comments_connection, dict):
        nodes_value = comments_connection.get("nodes")
        if isinstance(nodes_value, list):
            for comment_value in nodes_value:
                if not isinstance(comment_value, dict):
                    continue
                comments.append(_normalize_comment(comment_value))

    return {
        "id": thread_id,
        "comments": comments,
        "is_resolved": _is_thread_resolved(thread),
    }


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    comment_id_value = comment.get("id")
    comment_id = comment_id_value if isinstance(comment_id_value, str) else ""

    author_value = comment.get("author")
    login = ""
    if isinstance(author_value, dict):
        login_value = author_value.get("login")
        if isinstance(login_value, str):
            login = login_value

    body_value = comment.get("body")
    path_value = comment.get("path")

    return {
        "id": comment_id,
        "body": body_value if isinstance(body_value, str) else "",
        "path": path_value if isinstance(path_value, str) else "",
        "line": comment.get("line"),
        "original_line": comment.get("originalLine"),
        "user": {"login": login},
    }


def _reply_to_comment(
    pr: Any,
    comment_ctx: Mapping[str, Any] | CommentContext,
    text: str,
    debug: Callable[[int, str], None],
) -> None:
    """Reply to a PR comment (review comment or issue comment)."""
    context = _normalize_context(comment_ctx)

    event = context.event_name.lower()
    comment_id = context.id
    if not comment_id:
        pr.as_issue().create_comment(text)
        return

    try:
        if event == "pull_request_review_comment":
            post_pr_resource(pr, f"comments/{comment_id}/replies", {"body": text})
        else:
            pr.as_issue().create_comment(text)
    except Exception as exc:
        debug(1, f"Failed replying to comment {comment_id}: {exc}")


def _safe_reply(
    pr: Any,
    comment_ctx: Mapping[str, Any] | CommentContext | None,
    text: str,
    debug: Callable[[int, str], None],
) -> None:
    """Reply to a comment, silently logging failures."""
    if not comment_ctx:
        return
    try:
        _reply_to_comment(pr, comment_ctx, text, debug)
    except Exception as exc:
        debug(1, f"Failed to reply to comment: {exc}")


def _normalize_context(comment_ctx: Mapping[str, Any] | CommentContext) -> CommentContext:
    if isinstance(comment_ctx, CommentContext):
        return comment_ctx

    normalized = CommentContext.from_mapping(comment_ctx)
    if normalized is None:
        return CommentContext(id=0, event_name="")
    return normalized
