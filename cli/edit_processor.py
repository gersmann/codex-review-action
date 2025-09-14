from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from github import Github

from codex.config import (
    SandboxMode,
    SandboxWorkspaceWrite,
)

from .codex_client import CodexClient
from .config import ReviewConfig


class EditProcessor:
    """Handles edit command workflow for code review operations."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self.codex_client = CodexClient(config)

    def _debug(self, level: int, message: str) -> None:
        if self.config.debug_level >= level:
            print(f"[debug{level}] {message}", file=sys.stderr)

    def process_edit_command(
        self, command_text: str, pr_number: int, comment_ctx: dict | None = None
    ) -> int:
        """Run a coding-agent edit command against the PR's branch.

        High-level flow:
        - Build prompt and sandbox overrides
        - Execute the agent
        - If no changes: reply and exit
        - If dry run: show status, reply and exit
        - Otherwise: commit, push, reply
        Returns a process-like exit code: 0 success, 1 agent failure, 2 git failure.
        """

        self._debug(1, f"Edit command on PR #{pr_number}: {command_text[:120]}")

        # Fetch PR context
        repo, pr = self._get_repo_and_pr(pr_number)
        head_branch = pr.head.ref if pr.head else None

        # Build prompt + overrides
        prompt = self._build_edit_prompt(command_text)
        overrides = self._build_edit_overrides()

        # Execute the agent
        try:
            agent_output = self.codex_client.execute(prompt, config_overrides=overrides)
        except Exception as e:
            print(f"Edit execution failed: {e}", file=sys.stderr)
            self._safe_reply(repo, pr, comment_ctx, f"Edit failed: {e}")
            return 1

        # Determine change state early
        changed = self._git_has_changes()
        if not changed:
            print("No changes to commit.")
            self._safe_reply(
                repo,
                pr,
                comment_ctx,
                self._format_edit_reply(
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=self.config.dry_run,
                    changed=False,
                ),
            )
            return 0

        # Dry run: show status and exit
        if self.config.dry_run:
            print("DRY_RUN: would commit and push changes.")
            self._git_status_pretty()
            self._safe_reply(
                repo,
                pr,
                comment_ctx,
                self._format_edit_reply(
                    agent_output or "(no output)", pushed=False, dry_run=True, changed=True
                ),
            )
            return 0

        # Commit and push
        try:
            self._git_setup_identity()
            self._git_commit_all(f"Codex edit: {command_text.splitlines()[0][:72]}")
            if head_branch:
                self._git_push_head_to_branch(head_branch)
            else:
                subprocess.run(["git", "push"], check=True)
            print("Pushed edits successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Git operation failed: {e}", file=sys.stderr)
            self._safe_reply(repo, pr, comment_ctx, f"Git operation failed: {e}")
            return 2

        # Reply success
        self._safe_reply(
            repo,
            pr,
            comment_ctx,
            self._format_edit_reply(
                agent_output or "(no output)", pushed=True, dry_run=False, changed=True
            ),
        )
        return 0

    # ----- helpers to simplify control flow -----

    def _get_repo_and_pr(self, pr_number: int) -> tuple[Any, Any]:
        gh = Github(login_or_token=self.config.github_token, per_page=100)
        repo = gh.get_repo(f"{self.config.owner}/{self.config.repo_name}")
        pr = repo.get_pull(pr_number)
        return repo, pr

    def _build_edit_prompt(self, command_text: str) -> str:
        repo_root = self.config.repo_root or Path(".").resolve()
        base_instructions = (
            "You are a coding agent with write access to this repository.\n"
            f"Repository root: {repo_root}\n"
            "Follow the user's command below. Make focused changes with minimal diff.\n"
            "Use the apply_patch tool to edit files. Create directories/files as needed.\n"
            "Add or update small docs as necessary.\n"
            "Do not change unrelated code.\n"
        )
        instructions = base_instructions
        if self.config.act_instructions:
            instructions += f"\n{self.config.act_instructions}\n"
        return (
            f"{instructions}\n\nUser command:\n{command_text}\n\n"
            "When finished, ensure the repo builds/tests if applicable."
        )

    def _build_edit_overrides(self) -> dict:
        repo_root = self.config.repo_root or Path(".").resolve()
        sandbox_ws = SandboxWorkspaceWrite(
            writable_roots=[str(repo_root)],
            network_access=True,
            exclude_tmpdir_env_var=False,
            exclude_slash_tmp=False,
        )
        return {
            # Enable planning and patch application in ACT mode
            "include_plan_tool": True,
            "include_apply_patch_tool": True,
            # Overwrite any conservative defaults from the client
            "base_instructions": (
                "You are in ACT mode and MUST make the requested code changes. "
                "Use the apply_patch tool to edit files; keep diffs minimal and focused."
            ),
            # Writable sandbox over the repo
            "sandbox_mode": SandboxMode.DANGER_FULL_ACCESS,
            "sandbox_workspace_write": sandbox_ws,
        }

    def _safe_reply(self, repo: Any, pr: Any, comment_ctx: dict | None, text: str) -> None:
        if not comment_ctx:
            return
        try:
            self._reply_to_comment(repo, pr, comment_ctx, text)
        except Exception as e:
            self._debug(1, f"Failed to reply to comment: {e}")

    def _format_edit_reply(
        self, agent_output: str, *, pushed: bool, dry_run: bool, changed: bool
    ) -> str:
        status = (
            "dry-run (no push)"
            if dry_run
            else ("pushed changes" if pushed else ("no changes" if not changed else "not pushed"))
        )
        header = f"Codex edit result ({status}):"
        body = (agent_output or "").strip()
        if len(body) > 3500:
            body = body[:3500] + "\n\n… (truncated)"
        return f"{header}\n\n{body}"

    def _reply_to_comment(self, repo: Any, pr: Any, comment_ctx: dict, text: str) -> None:
        event = (comment_ctx.get("event_name") or "").lower()
        comment_id = int(comment_ctx.get("id") or 0)
        if not comment_id:
            pr.as_issue().create_comment(text)
            return
        try:
            if event == "pull_request_review_comment":
                url = f"{pr.url}/comments/{comment_id}/replies"
                pr._requester.requestJsonAndCheck("POST", url, input={"body": text})
            elif event == "issue_comment":
                pr.as_issue().create_comment(text)
            else:
                pr.as_issue().create_comment(text)
        except Exception as e:
            self._debug(1, f"Failed replying to comment {comment_id}: {e}")

    def _git_has_changes(self) -> bool:
        res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        return bool(res.stdout.strip())

    def _git_status_pretty(self) -> None:
        subprocess.run(["git", "status", "--short"], check=False)

    def _git_setup_identity(self) -> None:
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)

    def _git_commit_all(self, message: str) -> None:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)

    def _git_push_head_to_branch(self, branch: str) -> None:
        # Push current HEAD to the target branch name on origin (works in detached HEAD)
        subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)
