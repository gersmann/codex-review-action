from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .config import ReviewConfig
from .exceptions import PromptError


class PromptBuilder:
    """Handles prompt composition and guidelines loading."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config

    def _debug(self, level: int, message: str) -> None:
        if self.config.debug_level >= level:
            print(f"[debug{level}] {message}", file=sys.stderr)

    def load_guidelines(self) -> str:
        """Load review guidelines based on configuration strategy."""
        strategy = self.config.guidelines_strategy
        inline = self.config.guidelines_inline
        path = Path(self.config.guidelines_path)

        # Built-in file packaged with the action
        builtin_path = Path(__file__).resolve().parents[1] / "prompts" / "review.md"

        def read_file(file_path: Path) -> str:
            try:
                return file_path.read_text(encoding="utf-8")
            except Exception as e:
                self._debug(1, f"Failed reading prompt file {file_path}: {e}")
                raise PromptError(f"Failed to read guidelines file {file_path}: {e}") from e

        def file_exists_and_readable() -> bool:
            return path.exists() and path.is_file()

        # Strategy-specific loading
        if strategy == "inline" and inline:
            self._debug(1, "Using inline prompt (env)")
            return inline

        if strategy == "file" and file_exists_and_readable():
            self._debug(1, f"Using file prompt: {path}")
            return read_file(path)

        if strategy == "builtin":
            self._debug(1, f"Using builtin prompt: {builtin_path}")
            return read_file(builtin_path)

        # Auto strategy: prefer inline > file > builtin
        if inline:
            self._debug(1, "Using inline prompt (auto)")
            return inline

        if file_exists_and_readable():
            self._debug(1, f"Using file prompt (auto): {path}")
            return read_file(path)

        self._debug(1, f"Using builtin prompt (auto fallback): {builtin_path}")
        return read_file(builtin_path)

    def compose_prompt(
        self,
        guidelines: str,
        changed_files: list,
        pr_data: dict[str, Any],
    ) -> str:
        """Compose the complete review prompt."""
        repo_root = self.config.repo_root or Path(".").resolve()
        context_dir = repo_root / self.config.context_dir_name

        pr_title = pr_data.get("title") or ""
        head_label = pr_data.get("head", {}).get("label", "")
        base_label = pr_data.get("base", {}).get("label", "")
        head_sha = pr_data.get("head", {}).get("sha", "")
        base_sha = pr_data.get("base", {}).get("sha", "")

        intro = (
            "You are an autonomous code review assistant.\n"
            "Carefully read the guidelines and analyze ONLY the provided diffs.\n"
            "Output exactly the JSON as specified. Do not add fences or extra text.\n"
        )

        context = (
            f"PR Title: {pr_title}\n"
            f"From: {head_label} ({head_sha}) -> To: {base_label} ({base_sha})\n\n"
            "Important paths:\n"
            f"- Repo root (absolute): {repo_root}\n"
            f"- Local context dir: {context_dir} (contains combined_diffs.txt, pr.md, and per-file patches in diffs/)\n"
            "When returning code_location.absolute_file_path, use the absolute path under this root.\n"
            "Line ranges must overlap with the provided diff hunks.\n"
        )

        # Build diff content
        diffs: list[str] = []
        for file in changed_files:
            if not file.patch:
                continue
            diffs.append(
                f"File: {file.filename}\n"
                f"Status: {file.status}\n"
                f"Patch (unified diff):\n---\n{file.patch}\n"
            )

        diff_blob = (
            ("\n" + ("\n" + ("-" * 80) + "\n").join(diffs))
            if diffs
            else "\n(no diff patch content available)\n"
        )

        prompt = (
            f"{intro}\n\n"
            "Review guidelines (verbatim):\n"
            f"{guidelines}\n\n"
            f"{context}\n"
            "Changed files and patches:\n"
            f"{diff_blob}\n\n"
            "Respond now with the JSON schema output only."
        )

        return prompt
