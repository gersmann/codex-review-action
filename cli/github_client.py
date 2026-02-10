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


def _get_pr_resource(pr: Any, url_suffix: str) -> Any:
    """GET a PR sub-resource via PyGithub's internal requester."""
    url = f"{pr.url}/{url_suffix}"
    _, data = pr._requester.requestJsonAndCheck("GET", url)
    return data


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


def get_unresolved_threads(pr: Any) -> list[dict[str, Any]]:
    """Fetch unresolved review threads for a PR."""
    try:
        data = _get_pr_resource(pr, "threads")
    except Exception as exc:
        raise RuntimeError(f"fetch error for {pr.url}/threads: {exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError("unexpected /threads response type (expected list)")

    def is_resolved(thread: dict[str, Any]) -> bool:
        state = str(thread.get("state") or thread.get("resolution") or "").lower()
        return bool(
            thread.get("resolved")
            or thread.get("is_resolved")
            or thread.get("isResolved")
            or state in {"resolved", "completed", "dismissed"}
        )

    return [thread for thread in data if not is_resolved(thread)]


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
