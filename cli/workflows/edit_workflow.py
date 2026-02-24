from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from ..codex_client import CodexClient
from ..config import ReviewConfig, make_debug
from ..edit_prompt import build_edit_prompt, format_unresolved_threads_from_list
from ..git_ops import (
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
from ..github_client import GitHubClient, GitHubClientProtocol
from ..models import CommentContext


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
        comment_ctx: Mapping[str, Any] | CommentContext | None = None,
    ) -> int:
        """Run a coding-agent edit command against the PR's branch."""
        self._debug(1, f"Edit command on PR #{pr_number}: {command_text[:120]}")

        pr = self.github_client.get_pr(pr_number)
        head_branch = pr.head.ref if pr.head else None
        before_head_sha = git_current_head_sha()
        remote_head_sha = git_remote_head_sha(head_branch)
        normalized_comment_ctx = _normalize_comment_context(comment_ctx)
        before_snapshot = git_worktree_snapshot()

        unresolved_block = ""
        if self._wants_fix_unresolved(command_text):
            fetch_failed = False
            try:
                unresolved_threads = self.github_client.get_unresolved_threads(pr)
            except Exception as exc:
                fetch_failed = True
                warning = (
                    "Failed to retrieve review threads; continuing without unresolved-thread "
                    f"context: {exc}"
                )
                print(warning, file=sys.stderr)
                self._safe_reply(pr, normalized_comment_ctx, warning)
                unresolved_threads = []

            self._debug(1, f"Unresolved threads found: {len(unresolved_threads)}")
            if not fetch_failed and not unresolved_threads:
                msg = "No unresolved review threads detected; nothing to address."
                print(msg)
                self._safe_reply(pr, normalized_comment_ctx, msg)
                return 0

            if unresolved_threads:
                unresolved_block = format_unresolved_threads_from_list(unresolved_threads)

        prompt = build_edit_prompt(
            self.config,
            command_text,
            pr,
            normalized_comment_ctx,
            unresolved_block,
        )
        self._debug(1, f"Final Edit Prompt:\n{prompt}")
        try:
            agent_output = self.codex_client.execute(prompt, sandbox_mode="danger-full-access")
        except Exception as exc:
            print(f"Edit execution failed: {exc}", file=sys.stderr)
            self._safe_reply(pr, normalized_comment_ctx, f"Edit failed: {exc}")
            return 1

        if git_rebase_in_progress():
            msg = (
                "Git operation failed: repository is in an active rebase state. "
                "Resolve or abort the rebase before rerunning /codex."
            )
            print(msg, file=sys.stderr)
            self._safe_reply(pr, normalized_comment_ctx, msg)
            return 2

        changed = git_has_changes()
        after_snapshot = git_worktree_snapshot()
        agent_touched_paths = git_changed_paths_since_snapshot(before_snapshot, after_snapshot)
        try:
            ahead = git_head_is_ahead(pr.head.ref if pr.head else None)
        except Exception:
            ahead = False

        if not agent_touched_paths and not ahead:
            print("No agent-scoped changes to commit.")
            self._safe_reply(
                pr,
                normalized_comment_ctx,
                self._format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=self.config.dry_run,
                    changed=False,
                ),
            )
            return 0

        if not changed and not ahead:
            print("No changes to commit.")
            self._safe_reply(
                pr,
                normalized_comment_ctx,
                self._format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=self.config.dry_run,
                    changed=False,
                ),
            )
            return 0

        if self.config.dry_run:
            print("DRY_RUN: would commit and push changes.")
            git_status_pretty()
            self._safe_reply(
                pr,
                normalized_comment_ctx,
                self._format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=True,
                    changed=True,
                ),
            )
            return 0

        try:
            if changed and agent_touched_paths:
                git_setup_identity()
                summary = (
                    command_text.splitlines()[0] if command_text.splitlines() else command_text
                )
                git_commit_paths(f"Codex edit: {summary[:72]}", agent_touched_paths)

            after_head_sha = git_current_head_sha()
            history_rewritten = (
                before_head_sha is not None
                and after_head_sha is not None
                and before_head_sha != after_head_sha
                and not git_is_ancestor(before_head_sha, after_head_sha)
            )

            if head_branch:
                if history_rewritten:
                    self._debug(
                        1,
                        f"Detected rewritten history for {head_branch}; "
                        "using force-with-lease push.",
                    )
                    push_result = git_push_force_with_lease(head_branch, remote_head_sha)
                    if not push_result.ok:
                        raise subprocess.CalledProcessError(
                            push_result.returncode,
                            list(push_result.command),
                            push_result.stdout,
                            push_result.stderr,
                        )
                else:
                    git_push_head_to_branch(head_branch, self._debug)
            else:
                git_push()
            print("Pushed edits successfully.")
        except subprocess.CalledProcessError as exc:
            details = git_format_called_process_error(exc)
            print(f"Git operation failed:\n{details}", file=sys.stderr)
            self._safe_reply(
                pr,
                normalized_comment_ctx,
                f"Git operation failed:\n{details}",
            )
            return 2

        self._safe_reply(
            pr,
            normalized_comment_ctx,
            self._format_edit_reply(
                agent_output or "(no output)",
                pushed=True,
                dry_run=False,
                changed=True,
            ),
        )
        return 0

    def _safe_reply(self, pr: Any, comment_ctx: CommentContext | None, text: str) -> None:
        self.github_client.safe_reply(
            pr,
            comment_ctx,
            text,
            self._debug,
        )

    def _wants_fix_unresolved(self, text: str) -> bool:
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
        self,
        agent_output: str,
        *,
        pushed: bool,
        dry_run: bool,
        changed: bool,
        extra_summary: str | None = None,
    ) -> str:
        status = (
            "dry-run (no push)"
            if dry_run
            else ("pushed changes" if pushed else ("no changes" if not changed else "not pushed"))
        )
        header = f"Codex edit result ({status}):"
        body = agent_output.strip()
        if len(body) > 3500:
            body = body[:3500] + "\n\n… (truncated)"
        if extra_summary:
            return f"{header}\n\n{body}\n\n{extra_summary}"
        return f"{header}\n\n{body}"


def _normalize_comment_context(
    comment_ctx: Mapping[str, Any] | CommentContext | None,
) -> CommentContext | None:
    if isinstance(comment_ctx, CommentContext):
        return comment_ctx
    return CommentContext.from_mapping(comment_ctx)
