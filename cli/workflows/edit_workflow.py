from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ..clients.codex_client import CodexClient
from ..clients.git_ops import (
    GitWorktreeSnapshot,
    git_changed_paths_since_snapshot,
    git_commit_paths,
    git_current_head_sha,
    git_format_called_process_error,
    git_has_changes,
    git_head_is_ahead,
    git_is_ancestor,
    git_push,
    git_push_force_with_lease,
    git_push_head_to_branch,
    git_rebase_in_progress,
    git_remote_head_sha,
    git_setup_identity,
    git_status_pretty,
    git_worktree_snapshot,
)
from ..clients.github_client import GitHubClient, GitHubClientProtocol
from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import GitHubAPIError
from ..core.github_types import PullRequestLikeProtocol
from ..core.models import CommentContext, ReviewCommentSnapshot
from .edit_prompt import (
    build_comment_context_block,
    build_edit_prompt,
    format_unresolved_threads_from_list,
)


@dataclass(frozen=True)
class _EditPreflightState:
    head_branch: str | None
    before_head_sha: str | None
    remote_head_sha: str | None
    before_snapshot: GitWorktreeSnapshot


@dataclass(frozen=True)
class _EditPromptState:
    prompt: str
    comment_context_warning: str | None


@dataclass(frozen=True)
class _ReviewCommentContextState:
    comment_snapshot: ReviewCommentSnapshot | None = None
    parent_snapshot: ReviewCommentSnapshot | None = None
    warning: str | None = None


@dataclass(frozen=True)
class _EditPostAgentState:
    changed: bool
    agent_touched_paths: tuple[str, ...]
    ahead: bool


@dataclass(frozen=True)
class _EditEarlyExit:
    message: str
    exit_code: int


_DebugFn = Callable[[int, str], None]
_REBASE_IN_PROGRESS_MESSAGE = (
    "Git operation failed: repository is in an active rebase state. "
    "Resolve or abort the rebase before rerunning /codex."
)


class EditWorkflow:
    """Workflow for edit commands against PR branches."""

    def __init__(
        self,
        config: ReviewConfig,
        *,
        codex_client: CodexClient | None = None,
        github_client: GitHubClientProtocol | None = None,
    ) -> None:
        self.config = config
        self.codex_client = codex_client or CodexClient(config)
        self.github_client: GitHubClientProtocol = github_client or GitHubClient(config)
        self._debug = make_debug(config)

    def process_edit_command(
        self,
        command_text: str,
        pr_number: int,
        comment_ctx: CommentContext | None = None,
    ) -> int:
        """Run a coding-agent edit command against the PR's branch."""
        self._debug(1, f"Edit command on PR #{pr_number}: {command_text[:120]}")

        pr = self.github_client.get_pr(pr_number)
        preflight_state = self._collect_preflight_state_or_reply(pr, comment_ctx)
        if preflight_state is None:
            return 2

        prompt_state = _prepare_edit_prompt(
            config=self.config,
            github_client=self.github_client,
            debug=self._debug,
            command_text=command_text,
            pr=pr,
            comment_ctx=comment_ctx,
        )
        if isinstance(prompt_state, _EditEarlyExit):
            return self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=prompt_state.message,
                exit_code=prompt_state.exit_code,
                stderr=prompt_state.exit_code != 0,
            )
        if prompt_state.comment_context_warning:
            print(prompt_state.comment_context_warning, file=sys.stderr)
            self._debug(1, prompt_state.comment_context_warning)

        agent_output = self._execute_agent_turn_or_reply(
            pr,
            comment_ctx,
            prompt_state.prompt,
        )
        if agent_output is None:
            return 1

        post_agent_state = self._collect_post_agent_state_or_reply(pr, comment_ctx, preflight_state)
        if post_agent_state is None:
            return 2
        if isinstance(post_agent_state, _EditEarlyExit):
            return self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=post_agent_state.message,
                exit_code=post_agent_state.exit_code,
                stderr=True,
            )

        if not post_agent_state.agent_touched_paths and not post_agent_state.ahead:
            print("No agent-scoped changes to commit.")
            return _return_after_reply(
                github_client=self.github_client,
                debug=self._debug,
                pr=pr,
                comment_ctx=comment_ctx,
                text=_format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=self.config.dry_run,
                    changed=False,
                    extra_summary=prompt_state.comment_context_warning,
                ),
                exit_code=0,
            )

        if self.config.dry_run:
            print("DRY_RUN: would commit and push changes.")
            git_status_pretty()
            return _return_after_reply(
                github_client=self.github_client,
                debug=self._debug,
                pr=pr,
                comment_ctx=comment_ctx,
                text=_format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=True,
                    changed=True,
                    extra_summary=prompt_state.comment_context_warning,
                ),
                exit_code=0,
            )

        try:
            _finalize_git_edit(
                debug=self._debug,
                command_text=command_text,
                preflight_state=preflight_state,
                post_agent_state=post_agent_state,
            )
        except subprocess.CalledProcessError as exc:
            details = git_format_called_process_error(exc)
            return self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=f"Git operation failed:\n{details}",
                exit_code=2,
                stderr=True,
            )

        return _return_after_reply(
            github_client=self.github_client,
            debug=self._debug,
            pr=pr,
            comment_ctx=comment_ctx,
            text=_format_edit_reply(
                agent_output or "(no output)",
                pushed=True,
                dry_run=False,
                changed=True,
                extra_summary=prompt_state.comment_context_warning,
            ),
            exit_code=0,
        )

    def _report_and_reply(
        self,
        *,
        pr: PullRequestLikeProtocol,
        comment_ctx: CommentContext | None,
        message: str,
        exit_code: int,
        stderr: bool,
    ) -> int:
        if stderr:
            print(message, file=sys.stderr)
        else:
            print(message)
        return _return_after_reply(
            github_client=self.github_client,
            debug=self._debug,
            pr=pr,
            comment_ctx=comment_ctx,
            text=message,
            exit_code=exit_code,
        )

    def _collect_preflight_state_or_reply(
        self,
        pr: PullRequestLikeProtocol,
        comment_ctx: CommentContext | None,
    ) -> _EditPreflightState | None:
        try:
            preflight_state = _collect_preflight_state(pr)
            rebase_in_progress = git_rebase_in_progress()
        except subprocess.CalledProcessError as exc:
            details = git_format_called_process_error(exc)
            self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=f"Git state probe failed:\n{details}",
                exit_code=2,
                stderr=True,
            )
            return None
        if rebase_in_progress:
            self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=_REBASE_IN_PROGRESS_MESSAGE,
                exit_code=2,
                stderr=True,
            )
            return None
        return preflight_state

    def _execute_agent_turn_or_reply(
        self,
        pr: PullRequestLikeProtocol,
        comment_ctx: CommentContext | None,
        prompt: str,
    ) -> str | None:
        try:
            return self.codex_client.execute_text(
                prompt,
                sandbox_mode="danger-full-access",
            )
        except Exception as exc:
            self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=f"Edit failed: {exc}",
                exit_code=1,
                stderr=True,
            )
            return None

    def _collect_post_agent_state_or_reply(
        self,
        pr: PullRequestLikeProtocol,
        comment_ctx: CommentContext | None,
        preflight_state: _EditPreflightState,
    ) -> _EditPostAgentState | _EditEarlyExit | None:
        try:
            return _collect_post_agent_state(preflight_state)
        except subprocess.CalledProcessError as exc:
            details = git_format_called_process_error(exc)
            self._report_and_reply(
                pr=pr,
                comment_ctx=comment_ctx,
                message=f"Git state probe failed:\n{details}",
                exit_code=2,
                stderr=True,
            )
            return None


def _return_after_reply(
    *,
    github_client: GitHubClientProtocol,
    debug: _DebugFn,
    pr: PullRequestLikeProtocol,
    comment_ctx: CommentContext | None,
    text: str,
    exit_code: int,
) -> int:
    if comment_ctx is None:
        return exit_code

    try:
        if comment_ctx.event_name.lower() == "pull_request_review_comment":
            github_client.reply_to_review_comment(pr, comment_ctx.id, text)
        else:
            github_client.post_issue_comment(pr, text)
        reply_ok = True
    except GitHubAPIError as exc:
        warning = f"Failed to reply to comment {comment_ctx.id}: {exc}"
        print(warning, file=sys.stderr)
        debug(1, warning)
        reply_ok = False
    if not reply_ok and exit_code == 0:
        print(
            "GitHub reply delivery failed after a locally successful edit workflow result.",
            file=sys.stderr,
        )
    return exit_code


def _collect_preflight_state(pr: PullRequestLikeProtocol) -> _EditPreflightState:
    head_branch = pr.head.ref if pr.head else None
    return _EditPreflightState(
        head_branch=head_branch,
        before_head_sha=git_current_head_sha(),
        remote_head_sha=git_remote_head_sha(head_branch),
        before_snapshot=git_worktree_snapshot(),
    )


def _prepare_edit_prompt(
    *,
    config: ReviewConfig,
    github_client: GitHubClientProtocol,
    debug: _DebugFn,
    command_text: str,
    pr: PullRequestLikeProtocol,
    comment_ctx: CommentContext | None,
) -> _EditPromptState | _EditEarlyExit:
    unresolved_block = ""
    if _wants_fix_unresolved(command_text):
        try:
            unresolved_threads = github_client.get_unresolved_threads(pr)
        except GitHubAPIError as exc:
            warning = (
                "Failed to retrieve review threads; refusing to continue without "
                f"unresolved-thread context: {exc}"
            )
            return _EditEarlyExit(message=warning, exit_code=2)

        debug(1, f"Unresolved threads found: {len(unresolved_threads)}")
        if not unresolved_threads:
            return _EditEarlyExit(
                message="No unresolved review threads detected; nothing to address.",
                exit_code=0,
            )

        unresolved_block = format_unresolved_threads_from_list(unresolved_threads)

    review_comment_context = _load_review_comment_context(pr, comment_ctx)
    comment_context_result = build_comment_context_block(
        config,
        comment_ctx,
        review_comment_snapshot=review_comment_context.comment_snapshot,
        parent_review_comment_snapshot=review_comment_context.parent_snapshot,
        lookup_warning=review_comment_context.warning,
    )
    prompt = build_edit_prompt(
        config,
        command_text,
        pr,
        comment_context_result.block,
        unresolved_block,
    )
    debug(1, f"Edit prompt ready ({len(prompt)} chars)")
    debug(
        2,
        "Edit prompt context "
        f"unresolved_threads={'yes' if unresolved_block else 'no'} "
        f"comment_context={'yes' if comment_context_result.block else 'no'} "
        f"command_chars={len(command_text.strip())}",
    )
    return _EditPromptState(
        prompt=prompt,
        comment_context_warning=comment_context_result.warning,
    )


def _load_review_comment_context(
    pr: PullRequestLikeProtocol,
    comment_ctx: CommentContext | None,
) -> _ReviewCommentContextState:
    if comment_ctx is None or comment_ctx.event_name.lower() != "pull_request_review_comment":
        return _ReviewCommentContextState()

    comment_id = comment_ctx.id
    try:
        snapshot = ReviewCommentSnapshot.from_review_comment(pr.get_review_comment(comment_id))
    except Exception as exc:
        return _ReviewCommentContextState(
            warning=f"Comment context lookup failed for review comment {comment_id}: {exc}"
        )

    parent_snapshot: ReviewCommentSnapshot | None = None
    warning: str | None = None
    needs_parent_context = (not snapshot.path) or (
        snapshot.line is None and snapshot.original_line is None
    )
    if needs_parent_context and snapshot.in_reply_to_id is not None:
        parent_id = snapshot.in_reply_to_id
        try:
            parent_snapshot = ReviewCommentSnapshot.from_review_comment(
                pr.get_review_comment(parent_id)
            )
        except Exception as exc:
            warning = f"Failed to load parent review comment {parent_id}: {exc}"

    return _ReviewCommentContextState(
        comment_snapshot=snapshot,
        parent_snapshot=parent_snapshot,
        warning=warning,
    )


def _collect_post_agent_state(
    preflight_state: _EditPreflightState,
) -> _EditPostAgentState | _EditEarlyExit:
    if git_rebase_in_progress():
        return _EditEarlyExit(
            message=_REBASE_IN_PROGRESS_MESSAGE,
            exit_code=2,
        )

    changed = git_has_changes()
    after_snapshot = git_worktree_snapshot()
    return _EditPostAgentState(
        changed=changed,
        agent_touched_paths=tuple(
            git_changed_paths_since_snapshot(
                preflight_state.before_snapshot,
                after_snapshot,
            )
        ),
        ahead=git_head_is_ahead(preflight_state.head_branch),
    )


def _finalize_git_edit(
    *,
    debug: _DebugFn,
    command_text: str,
    preflight_state: _EditPreflightState,
    post_agent_state: _EditPostAgentState,
) -> None:
    if post_agent_state.changed and post_agent_state.agent_touched_paths:
        git_setup_identity()
        summary = command_text.splitlines()[0] if command_text.splitlines() else command_text
        git_commit_paths(f"Codex edit: {summary[:72]}", post_agent_state.agent_touched_paths)

    after_head_sha = git_current_head_sha()
    history_rewritten = (
        preflight_state.before_head_sha is not None
        and after_head_sha is not None
        and preflight_state.before_head_sha != after_head_sha
        and not git_is_ancestor(preflight_state.before_head_sha, after_head_sha)
    )

    if preflight_state.head_branch:
        if history_rewritten:
            debug(
                1,
                f"Detected rewritten history for {preflight_state.head_branch}; "
                "using force-with-lease push.",
            )
            git_push_force_with_lease(
                preflight_state.head_branch,
                preflight_state.remote_head_sha,
            )
        else:
            git_push_head_to_branch(preflight_state.head_branch, debug)
    else:
        git_push()

    print("Pushed edits successfully.")


def _wants_fix_unresolved(text: str) -> bool:
    """Detect intent to address review comments with a minimal heuristic."""
    if not text:
        return False

    normalized = " ".join(text.lower().split())
    if re.search(r"\b(do\s+not|don't|dont)\s+(address|fix|resolve)\b", normalized):
        return False

    has_verb = bool(re.search(r"\b(address|fix|resolve)\b", normalized))
    has_noun = bool(
        re.search(
            r"\b((review\s+)?comments?|((review\s+)?threads?)|feedback|reviews?)\b",
            normalized,
        )
    )
    return has_verb and has_noun


def _format_edit_reply(
    agent_output: str,
    *,
    pushed: bool,
    dry_run: bool,
    changed: bool,
    extra_summary: str | None = None,
) -> str:
    if dry_run:
        status = "dry-run (no push)"
    elif pushed:
        status = "pushed changes"
    elif not changed:
        status = "no changes"
    else:
        status = "not pushed"
    header = f"Codex edit result ({status}):"
    body = agent_output.strip()
    if len(body) > 3500:
        body = body[:3500] + "\n\n… (truncated)"
    if extra_summary:
        return f"{header}\n\n{body}\n\n{extra_summary}"
    return f"{header}\n\n{body}"
