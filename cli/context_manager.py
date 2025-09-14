from __future__ import annotations

from pathlib import Path
from typing import Any

from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from .patch_parser import annotate_patch_with_line_numbers


class ContextManager:
    """Manages context artifacts for code review operations."""

    def write_context_artifacts(
        self,
        pr: Any,
        changed_files: list[Any],
        repo_root: Path,
        context_dir_name: str = ".codex-context",
    ) -> None:
        """Create a context directory with diffs and PR context (including comments)."""
        base_dir = (repo_root / context_dir_name).resolve()

        diffs_dir = base_dir / "diffs"
        annotated_dir = base_dir / "diffs_annotated"
        diffs_dir.mkdir(parents=True, exist_ok=True)
        annotated_dir.mkdir(parents=True, exist_ok=True)

        # Write combined diffs file and per-file patches
        combined_lines: list[str] = []
        for file in changed_files:
            filename = file.filename
            patch = getattr(file, "patch", None)
            status = getattr(file, "status", "modified")
            if not filename or not patch:
                continue

            combined_lines.append(f"File: {filename}\nStatus: {status}\n---\n{patch}\n")

            # Create subdirs mirroring the file path and write .patch
            file_path = Path(filename)
            target_dir = diffs_dir / file_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{file_path.name}.patch").write_text(patch, encoding="utf-8")

            # Also write annotated diff with explicit BASE/HEAD numbers
            a_target_dir = annotated_dir / file_path.parent
            a_target_dir.mkdir(parents=True, exist_ok=True)
            annotated = annotate_patch_with_line_numbers(patch)
            (a_target_dir / f"{file_path.name}.annotated.patch").write_text(
                annotated, encoding="utf-8"
            )

        (base_dir / "combined_diffs.txt").write_text(
            "\n" + ("\n" + ("-" * 80) + "\n").join(combined_lines), encoding="utf-8"
        )

        # Write PR metadata and comments into pr.md
        self._write_pr_metadata(pr, base_dir)

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

        # Issue comments (a.k.a. conversation comments)
        try:
            issue_comments = list(pr.get_issue_comments())
        except Exception:
            issue_comments = []
        if issue_comments:
            parts.append("Issue Comments:")
            for c in issue_comments:
                if isinstance(c, IssueComment):
                    author = c.user.login if c.user else ""
                    created = c.created_at
                    parts.append(f"- [{created}] @{author}:\n{c.body or ''}\n")

        # Review comments (inline on diffs)
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            review_comments = []
        if review_comments:
            parts.append("Review Comments:")
            for rc in review_comments:
                if isinstance(rc, PullRequestComment):
                    author = rc.user.login if rc.user else ""
                    created = rc.created_at
                    path = rc.path or ""
                    line = rc.line or rc.original_line
                    parts.append(f"- [{created}] @{author} on {path}:{line}\n{rc.body or ''}\n")

        (base_dir / "pr.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
