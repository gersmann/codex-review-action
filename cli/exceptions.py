from __future__ import annotations


class CodexReviewError(Exception):
    """Base exception for codex review operations."""

    pass


class GitHubAPIError(CodexReviewError):
    """GitHub API related errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ConfigurationError(CodexReviewError):
    """Configuration validation errors."""

    pass


class PatchParsingError(CodexReviewError):
    """Patch parsing related errors."""

    pass


class CodexExecutionError(CodexReviewError):
    """Codex execution related errors."""

    pass


class PromptError(CodexReviewError):
    """Prompt composition or loading errors."""

    pass
