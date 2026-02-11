from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from github.File import File
from github.PullRequest import PullRequest

from .config import ReviewConfig, make_debug
from .exceptions import PromptError


class PromptBuilder:
    """Handles prompt composition and guidelines loading."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self._debug = make_debug(config)

    def load_guidelines(self) -> str:
        """Load review guidelines from the built-in prompts/review.md file.

        Only used in review mode. Act mode doesn't use guidelines.
        """
        if self.config.mode != "review":
            return ""

        builtin_path = Path(__file__).resolve().parents[1] / "prompts" / "review.md"

        try:
            self._debug(1, f"Using built-in prompt: {builtin_path}")
            return builtin_path.read_text(encoding="utf-8")
        except Exception as exc:
            self._debug(1, f"Failed reading built-in prompt file {builtin_path}: {exc}")
            raise PromptError(
                f"Failed to read built-in guidelines file {builtin_path}: {exc}"
            ) from exc

    def compose_prompt(
        self,
        changed_files: Sequence[File],
        pr: PullRequest,
    ) -> str:
        """Compose the complete review prompt."""
        repo_root = self.config.repo_root or Path(".").resolve()
        context_dir = repo_root / self.config.context_dir_name

        context = (
            "<pull_request>\n"
            f"<title>{pr.title}</title>\n"
            f'<head label="{pr.head.label}" sha="{pr.head.sha}" ref="{pr.head.ref}"/>\n'
            f'<base label="{pr.base.label}" sha="{pr.base.sha}" ref="{pr.base.ref}"/>\n'
            "</pull_request>\n"
            "<paths>\n"
            f"<repo_root>{repo_root}</repo_root>\n"
            f"<context_dir>{context_dir}</context_dir>\n"
            "</paths>\n"
        )

        line_rules = (
            "<line_rules>\n"
            "- Always use HEAD (right side) line numbers for code_location.\n"
            "- Prefer the exact added line(s) that contain the problematic code/text, not surrounding blanks.\n"
            "- Never select a trailing blank line. If your intended target is a blank line, shift to the nearest non-blank line (prefer earlier).\n"
            "- Keep ranges minimal; for single-line issues, set start=end to the single non-blank line.\n"
            "- Your line_range must overlap a visible + or context line in the diff hunk.\n"
            "- When you quote text in the body, align code_location.start to the line that contains that quote.\n"
            "</line_rules>\n"
        )

        changed_list: list[str] = []
        for file in changed_files:
            filename = file.filename
            if not filename:
                continue
            status = file.status
            changed_list.append(f"- {filename} ({status})")

        changed_summary = (
            "<changed_files>\n" + "\n".join(changed_list) + "\n</changed_files>\n"
            if changed_list
            else "<changed_files>(none)</changed_files>\n"
        )

        try:
            context_dir_rel = context_dir.relative_to(repo_root)
        except ValueError:
            context_dir_rel = Path(self.config.context_dir_name)

        pr_metadata_rel = context_dir_rel / "pr.md"
        review_comments_rel = context_dir_rel / "review_comments.md"

        context_artifacts = (
            "<context_artifacts>\n"
            f"<pr_metadata>{pr_metadata_rel}</pr_metadata>\n"
            f"<review_comments>{review_comments_rel}</review_comments>\n"
            "</context_artifacts>\n"
        )

        base_ref = pr.base.ref or pr.base.label.split(":", 1)[-1]
        review_instructions = (
            "<git_review_instructions>\n"
            "To view the exact changes to be merged, run:\n"
            f"  git diff origin/{base_ref}...HEAD\n"
            "(Triple dots compute the merge base automatically.)\n"
            "Consult {review_comments_rel} for context and provide prioritized, actionable findings.\n"
            "</git_review_instructions>\n"
        ).format(review_comments_rel=review_comments_rel)

        extra_instructions = ""
        extra = self.config.additional_prompt
        if extra:
            extra_instructions = (
                "<additional_instructions>\n" + extra + "\n</additional_instructions>\n"
            )

        prompt = (
            f"{context}"
            f"{context_artifacts}"
            f"{changed_summary}"
            f"{review_instructions}"
            f"{line_rules}"
            f"{extra_instructions}"
            "<response_format>Respond now with the JSON schema output only.</response_format>"
        )

        return prompt
