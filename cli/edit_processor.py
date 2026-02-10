from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from github import Github

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

        # Build prompt inputs (unresolved threads if requested)
        if self._wants_fix_unresolved(command_text):
            try:
                threads = self._get_unresolved_threads(pr)
            except Exception as e:
                err = f"Failed to retrieve review threads: {e}"
                print(err, file=sys.stderr)
                self._safe_reply(repo, pr, comment_ctx, err)
                return 1
            self._debug(1, f"Unresolved threads found: {len(threads)}")
            if not threads:
                msg = "No unresolved review threads detected; nothing to address."
                print(msg)
                self._safe_reply(repo, pr, comment_ctx, msg)
                return 0

        # Build prompt + overrides
        prompt = self._build_edit_prompt(command_text, pr, comment_ctx)
        overrides = self._build_edit_overrides()

        # Execute the agent
        try:
            agent_output = self.codex_client.execute(prompt, config_overrides=overrides)
        except Exception as e:
            # Surface agent errors without stack noise; upstream already logs details
            print(f"Edit execution failed: {e}", file=sys.stderr)
            self._safe_reply(repo, pr, comment_ctx, f"Edit failed: {e}")
            return 1

        # Determine change state early. In some environments a commit may have
        # already been created (clean worktree), but not pushed. Detect that
        # and push even when there are no staged changes.
        changed = self._git_has_changes()
        try:
            ahead = self._git_head_is_ahead(pr.head.ref if pr.head else None)
        except Exception:
            # Non-fatal: fall back to change-based behavior
            ahead = False
        if not changed and not ahead:
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
                    agent_output or "(no output)",
                    pushed=False,
                    dry_run=True,
                    changed=True,
                ),
            )
            return 0

        # Commit (if needed) and push
        try:
            if changed:
                self._git_setup_identity()
                self._git_commit_all(f"Codex edit: {command_text.splitlines()[0][:72]}")
            # Push even if worktree is clean but HEAD is ahead
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

    def _build_edit_prompt(self, command_text: str, pr: Any, comment_ctx: dict | None) -> str:
        repo_root = self.config.repo_root or Path(".").resolve()
        head_ref = getattr(getattr(pr, "head", None), "ref", "") or "HEAD"
        base_ref = getattr(getattr(pr, "base", None), "ref", "") or "main"

        sections: list[str] = []
        sections.append(
            "<act_overview>\n"
            "You are a coding agent with write access to this repository.\n"
            f"Repository root: {repo_root}\n"
            "Make the requested change with the smallest reasonable diff.\n"
            "Use the apply_patch tool to edit files. Create files/dirs if needed.\n"
            "Do not change unrelated code.\n"
            "</act_overview>\n"
        )

        # Optional operator-provided guidance (env/inputs)
        extra = (self.config.act_instructions or "").strip()
        if extra:
            sections.append("<extra_instructions>\n" + extra + "\n</extra_instructions>\n")

        # Include PR/branch context for git-based searches if needed
        sections.append(
            f"<pr_context>\n<head>{head_ref}</head>\n<base>{base_ref}</base>\n</pr_context>\n"
        )

        # Optionally include unresolved review threads if the command asks to fix them
        if self._wants_fix_unresolved(command_text):
            unresolved_block = self._format_unresolved_threads(pr)
            if unresolved_block:
                sections.append(unresolved_block)

        # Include review comment context when available
        cc_block = self._format_comment_context(pr, comment_ctx)
        if cc_block:
            sections.append(cc_block)

        # User instruction last
        sections.append("<edit_request>\n" + command_text.strip() + "\n</edit_request>\n")

        sections.append(
            "<completion_rules>\n"
            "- Apply the change and ensure the project still type-checks/builds if applicable.\n"
            "- Keep diffs minimal and focused on the request.\n"
            "</completion_rules>\n"
        )

        return "".join(sections)

    def _wants_fix_unresolved(self, text: str) -> bool:
        """Detect intent to address review comments with a minimal heuristic.

        Triggers when BOTH are present (case-insensitive):
        - verb: address|fix|resolve
        - noun: comment(s)|(review )?thread(s)|feedback|review(s)

        Simple negation guard blocks patterns like "do not address" or "don't fix".
        """
        if not text:
            return False

        t = " ".join(text.lower().split())

        # Negation guard
        if re.search(r"\b(do\s+not|don't|dont)\s+(address|fix|resolve)\b", t):
            return False

        has_verb = bool(re.search(r"\b(address|fix|resolve)\b", t))
        has_noun = bool(
            re.search(r"\b((review\s+)?comments?|((review\s+)?threads?)|feedback|reviews?)\b", t)
        )
        return has_verb and has_noun

    def _format_unresolved_threads(self, pr: Any) -> str:
        # Backward-compatible helper: fetch and format
        threads = self._get_unresolved_threads(pr)
        if not threads:
            return ""
        return self._format_unresolved_threads_from_list(threads)

    def _get_unresolved_threads(self, pr: Any) -> list[dict[str, Any]]:
        url = f"{pr.url}/threads"
        try:
            _, data = pr._requester.requestJsonAndCheck("GET", url)
        except Exception as e:
            raise RuntimeError(f"fetch error for {url}: {e}") from e
        if not isinstance(data, list):
            raise RuntimeError("unexpected /threads response type (expected list)")
        raw_threads = data

        def is_resolved(th: dict[str, Any]) -> bool:
            state = str(th.get("state") or th.get("resolution") or "").lower()
            return bool(
                th.get("resolved")
                or th.get("is_resolved")
                or th.get("isResolved")
                or state in {"resolved", "completed", "dismissed"}
            )

        return [th for th in raw_threads if not is_resolved(th)]

    def _format_unresolved_threads_from_list(self, threads: list[dict[str, Any]]) -> str:
        items: list[str] = []
        for th in threads:
            try:
                tid = th.get("id") or th.get("node_id") or ""
                comments = th.get("comments") or []
                if not isinstance(comments, list):
                    continue
                entry_lines: list[str] = [f'<thread id="{tid}">']
                for c in comments:
                    cid = c.get("id") or ""
                    author = ((c.get("user") or {}).get("login")) or ""
                    path = c.get("path") or ""
                    line = c.get("line") or c.get("original_line") or ""
                    body = (c.get("body") or "").strip()
                    entry_lines.append(
                        f'<comment id="{cid}" author="{author}" path="{path}" line="{line}">\n{body}\n</comment>'
                    )
                entry_lines.append("</thread>")
                items.append("\n".join(entry_lines))
            except Exception:
                continue

        if not items:
            return ""
        header = (
            "<unresolved_comments>\n"
            "These are the UNRESOLVED review threads. For each, make the smallest code change that addresses the feedback.\n"
            "Do not mark threads resolved; just apply code fixes.\n"
        )
        return header + "\n".join(items) + "\n</unresolved_comments>\n"

    def _format_comment_context(self, pr: Any, comment_ctx: dict | None) -> str:
        if not comment_ctx:
            return ""
        event = (comment_ctx.get("event_name") or "").lower()
        cid = int(comment_ctx.get("id") or 0)
        if not cid:
            return ""

        try:
            if event == "pull_request_review_comment":
                rc = pr.get_review_comment(cid)
                path = getattr(rc, "path", "") or ""
                line = getattr(rc, "line", None)
                orig_line = getattr(rc, "original_line", None)
                in_reply_to_id = getattr(rc, "in_reply_to_id", None)
                # If this is a reply lacking position info, try the parent comment
                if (not path or (line is None and orig_line is None)) and in_reply_to_id:
                    try:
                        parent = pr.get_review_comment(int(in_reply_to_id))
                        path = path or getattr(parent, "path", "") or ""
                        line = line or getattr(parent, "line", None)
                        orig_line = orig_line or getattr(parent, "original_line", None)
                    except Exception:
                        pass
                diff_hunk = getattr(rc, "diff_hunk", "") or ""
                commit_id = getattr(rc, "commit_id", "") or ""

                excerpt = self._read_file_excerpt(path, line or orig_line or 0)
                return (
                    '<comment_context type="pull_request_review_comment">\n'
                    f"<id>{cid}</id>\n"
                    f"<path>{path}</path>\n"
                    f"<line>{line or ''}</line>\n"
                    f"<original_line>{orig_line or ''}</original_line>\n"
                    f"<commit>{commit_id}</commit>\n"
                    "<diff_hunk>\n"
                    + diff_hunk
                    + "\n</diff_hunk>\n"
                    + excerpt
                    + "</comment_context>\n"
                )
            elif event == "issue_comment":
                # For issue comments, we only include the body; no path/line available.
                body = str(comment_ctx.get("body") or "")
                return (
                    '<comment_context type="issue_comment">\n'
                    f"<id>{cid}</id>\n"
                    "<note>No file/line associated with this comment. If the edit targets a specific file, infer from the repository structure or the instruction text.</note>\n"
                    "<body>\n" + body + "\n</body>\n"
                    "</comment_context>\n"
                )
        except Exception:
            return ""
        return ""

    def _read_file_excerpt(self, rel_path: str, focus_line: int, context: int = 30) -> str:
        if not rel_path:
            return ""
        try:
            repo_root = self.config.repo_root or Path(".").resolve()
            abs_path = (repo_root / rel_path).resolve()
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            n = len(lines)
            if focus_line <= 0:
                start = 1
                end = min(n, 2 * context)
            else:
                start = max(1, focus_line - context)
                end = min(n, focus_line + context)
            # Build numbered excerpt
            buf = [f'<file_excerpt path="{rel_path}" start="{start}" end="{end}">\n']
            for i in range(start, end + 1):
                code = lines[i - 1]
                buf.append(f"{i:>6}: {code}")
            buf.append("\n</file_excerpt>\n")
            return "\n".join(buf)
        except Exception:
            return ""

    def _build_edit_overrides(self) -> dict[str, Any]:
        return {
            # Enable planning and patch application in ACT mode
            "include_plan_tool": True,
            # Overwrite any conservative defaults from the client
            "base_instructions": (
                "You are in ACT mode and MUST make the requested code changes. "
                "Use the apply_patch tool to edit files; keep diffs minimal and focused. "
                "Use <comment_context> (path/line/diff) and <file_excerpt> to locate the change precisely."
            ),
            # Writable sandbox over the repo
            "sandbox_mode": "danger-full-access",
        }

    def _safe_reply(self, repo: Any, pr: Any, comment_ctx: dict | None, text: str) -> None:
        if not comment_ctx:
            return
        try:
            self._reply_to_comment(repo, pr, comment_ctx, text)
        except Exception as e:
            self._debug(1, f"Failed to reply to comment: {e}")

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
        body = (agent_output or "").strip()
        if len(body) > 3500:
            body = body[:3500] + "\n\n… (truncated)"
        if extra_summary:
            return f"{header}\n\n{body}\n\n{extra_summary}"
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
            [
                "git",
                "config",
                "user.email",
                "github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)

    def _git_commit_all(self, message: str) -> None:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)

    def _git_push_head_to_branch(self, branch: str) -> None:
        """Push HEAD to the target branch, retrying after a rebase if needed."""

        push_cmd = ["git", "push", "origin", f"HEAD:refs/heads/{branch}"]
        result = subprocess.run(push_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return

        self._debug(1, f"Push rejected for {branch}; attempting fetch/rebase.")

        fetch = subprocess.run(["git", "fetch", "origin", branch], capture_output=True, text=True)
        if fetch.returncode != 0:
            raise subprocess.CalledProcessError(
                fetch.returncode,
                fetch.args,
                fetch.stdout,
                fetch.stderr or result.stderr,
            )

        rebase_target = f"origin/{branch}"
        rebase = subprocess.run(["git", "rebase", rebase_target], capture_output=True, text=True)
        if rebase.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], check=False)
            raise subprocess.CalledProcessError(
                rebase.returncode,
                rebase.args,
                rebase.stdout,
                rebase.stderr,
            )

        final_push = subprocess.run(push_cmd, capture_output=True, text=True)
        if final_push.returncode != 0:
            raise subprocess.CalledProcessError(
                final_push.returncode,
                final_push.args,
                final_push.stdout,
                final_push.stderr,
            )

    def _git_head_is_ahead(self, branch: str | None) -> bool:
        """Return True if HEAD has commits not present on the remote branch.

        If branch is None, compare against the upstream of HEAD when available.
        Tolerates missing remote refs by considering HEAD ahead (so a push will
        create the branch).
        """
        # Identify the remote ref to compare against
        ref = None
        if branch:
            ref = f"origin/{branch}"
        else:
            # Try to resolve upstream; ignore errors
            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                ref = res.stdout.strip()

        # If we still don't have a ref, assume ahead to force a push
        if not ref:
            return True

        # If remote ref doesn't exist yet, treat as ahead
        ls = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch or ""],
            capture_output=True,
        )
        if ls.returncode != 0:
            return True

        # Compute ahead/behind counts
        comp = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...{ref}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if comp.returncode != 0:
            return False
        parts = (comp.stdout.strip() or "0\t0").split()
        try:
            ahead = int(parts[0]) if parts else 0
        except ValueError:
            ahead = 0
        return ahead > 0
