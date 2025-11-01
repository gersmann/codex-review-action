from __future__ import annotations

from pathlib import Path
from typing import Any

from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment


class ContextManager:
    """Manages context artifacts for code review operations."""

    def write_context_artifacts(
        self,
        pr: PullRequest,
        repo_root: Path,
        context_dir_name: str = ".codex-context",
    ) -> None:
        """Create a context directory with PR metadata and discussion context."""
        base_dir = (repo_root / context_dir_name).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)

        self._write_pr_metadata(pr, base_dir)
        self._write_review_comments(pr, base_dir)

    def _write_pr_metadata(self, pr: Any, base_dir: Path) -> None:
        """Write PR metadata and comments into pr.md."""
        parts: list[str] = []
        parts.append(f"PR #{pr.number}: {pr.title or ''}")
        parts.append("")
        parts.append(f"URL: {pr.html_url}")
        parts.append(f"Author: {pr.user.login if pr.user else ''}")
        parts.append(f"State: {pr.state}")
        parts.append("")
        body = pr.body or ""
        if body:
            parts.append("PR Description:\n")
            parts.append(body)
            parts.append("")

        (base_dir / "pr.md").write_text("\n".join(parts) + "\n", encoding="utf-8")

    def _write_review_comments(self, pr: Any, base_dir: Path) -> None:
        """Write issue-level and inline review comments to review_comments.md."""

        lines: list[str] = []

        # Issue comments (a.k.a. conversation comments)
        try:
            issue_comments = list(pr.get_issue_comments())
        except Exception:
            issue_comments = []
        if issue_comments:
            lines.append("Issue Comments:")
            for c in issue_comments:
                if isinstance(c, IssueComment):
                    author = c.user.login if c.user else ""
                    created = c.created_at
                    lines.append(f"- [{created}] @{author}:")
                    lines.append(c.body or "")
                    lines.append("")

        # Review comments (inline on diffs)
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            review_comments = []
        if review_comments:
            lines.append("Inline Review Comments:")
            for rc in review_comments:
                if isinstance(rc, PullRequestComment):
                    author = rc.user.login if rc.user else ""
                    created = rc.created_at
                    path = rc.path or ""
                    line = rc.line or rc.original_line
                    lines.append(f"- [{created}] @{author} on {path}:{line}")
                    lines.append(rc.body or "")
                    lines.append("")

        if not lines:
            lines.append("(no review comments available)")

        (base_dir / "review_comments.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
