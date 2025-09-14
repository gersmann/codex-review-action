from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError


@dataclass
class ReviewConfig:
    """Configuration for code review operations."""

    # GitHub configuration
    github_token: str
    repository: str
    pr_number: int | None = None

    # Model configuration
    model_provider: str = "openai"
    model_name: str = "gpt-4.1-mini"
    reasoning_effort: str = "medium"

    # Review configuration
    guidelines_strategy: str = "auto"
    guidelines_path: str = "prompts/code-review.md"
    guidelines_inline: str = ""

    # Output configuration
    debug_level: int = 0
    stream_output: bool = True
    dry_run: bool = False

    # Repository paths
    repo_root: Path | None = None

    @classmethod
    def from_environment(cls) -> ReviewConfig:
        """Create configuration from environment variables."""
        # GitHub configuration
        github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not github_token:
            raise ConfigurationError("GITHUB_TOKEN not provided")

        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not repository or "/" not in repository:
            raise ConfigurationError("GITHUB_REPOSITORY missing or invalid")

        # Try to get PR number from environment
        pr_number = None
        if pr_num_str := os.environ.get("PR_NUMBER"):
            try:
                pr_number = int(pr_num_str)
            except ValueError as e:
                raise ConfigurationError(f"Invalid PR_NUMBER: {pr_num_str}") from e

        # Model configuration
        model_provider = os.environ.get("CODEX_PROVIDER", "openai").strip().lower()
        model_name = os.environ.get("CODEX_MODEL", "gpt-4.1-mini").strip()
        reasoning_effort = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()

        # Validate model authentication
        if model_provider == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                raise ConfigurationError("Missing OPENAI_API_KEY for model provider 'openai'")

        # Guidelines configuration
        guidelines_strategy = (os.environ.get("REVIEW_PROMPT_STRATEGY") or "auto").strip().lower()
        guidelines_path = os.environ.get("REVIEW_PROMPT_PATH") or "prompts/code-review.md"
        guidelines_inline = os.environ.get("REVIEW_PROMPT_INLINE") or ""

        # Output configuration
        debug_level = _parse_debug_level(os.environ.get("DEBUG_CODEREVIEW", "0"))
        stream_output = os.environ.get("STREAM_AGENT_MESSAGES", "1") != "0"
        dry_run = os.environ.get("DRY_RUN") == "1"

        # Repository paths
        repo_root = None
        if workspace := os.environ.get("GITHUB_WORKSPACE"):
            repo_root = Path(workspace).resolve()

        return cls(
            github_token=github_token,
            repository=repository,
            pr_number=pr_number,
            model_provider=model_provider,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            guidelines_strategy=guidelines_strategy,
            guidelines_path=guidelines_path,
            guidelines_inline=guidelines_inline,
            debug_level=debug_level,
            stream_output=stream_output,
            dry_run=dry_run,
            repo_root=repo_root,
        )

    @classmethod
    def from_args(cls, **kwargs: Any) -> ReviewConfig:
        """Create configuration from keyword arguments."""
        # Start with environment defaults
        try:
            config = cls.from_environment()
        except ConfigurationError:
            # If environment config fails, create minimal config
            config = cls(github_token="", repository="")

        # Override with provided arguments
        for key, value in kwargs.items():
            if hasattr(config, key) and value is not None:
                setattr(config, key, value)

        return config

    def validate(self) -> None:
        """Validate the configuration."""
        if not self.github_token:
            raise ConfigurationError("GitHub token is required")

        if not self.repository or "/" not in self.repository:
            raise ConfigurationError("Repository must be in format 'owner/repo'")

        if self.pr_number is not None and self.pr_number <= 0:
            raise ConfigurationError("PR number must be positive")

        if self.guidelines_strategy not in ("auto", "inline", "file", "builtin"):
            raise ConfigurationError(f"Invalid guidelines strategy: {self.guidelines_strategy}")

        if self.debug_level < 0:
            raise ConfigurationError("Debug level must be non-negative")

    @property
    def owner(self) -> str:
        """Extract owner from repository string."""
        return self.repository.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        """Extract repository name from repository string."""
        return self.repository.split("/", 1)[1]


def _parse_debug_level(value: str) -> int:
    """Parse debug level from string with fallback."""
    try:
        return int(value.strip() or "0")
    except (ValueError, AttributeError):
        return 0
