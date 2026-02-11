from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError

_CONFIG_OVERRIDE_KEYS = frozenset(
    {
        "github_token",
        "repository",
        "pr_number",
        "mode",
        "model_provider",
        "model_name",
        "reasoning_effort",
        "fast_model_name",
        "fast_reasoning_effort",
        "act_instructions",
        "debug_level",
        "stream_output",
        "dry_run",
        "additional_prompt",
        "repo_root",
        "context_dir_name",
    }
)


@dataclass
class ReviewConfig:
    """Configuration for code review operations."""

    # GitHub configuration
    github_token: str
    repository: str
    pr_number: int | None = None

    # Mode configuration
    mode: str = "review"  # "review" or "act"

    # Model configuration
    model_provider: str = "openai"
    model_name: str = "gpt-5"
    reasoning_effort: str = "medium"
    # Fast model for deduplication on repeated runs (review mode only)
    fast_model_name: str = "gpt-5-mini"
    fast_reasoning_effort: str = "low"

    # Act mode configuration
    act_instructions: str = ""

    # Output configuration
    debug_level: int = 0
    stream_output: bool = True
    dry_run: bool = False
    additional_prompt: str = ""

    # Repository paths
    repo_root: Path | None = None
    context_dir_name: str = ".codex-context"

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

        # fail on invalid input parameters & do not mask config errors!

        mode = os.environ.get("CODEX_MODE", "review").strip()
        # Model configuration
        model_provider = os.environ.get("CODEX_PROVIDER", "openai").strip()
        model_name = os.environ.get("CODEX_MODEL", "gpt-5").strip()
        reasoning_effort = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()
        fast_model_name = os.environ.get("CODEX_FAST_MODEL", model_name).strip()
        fast_reasoning_effort = os.environ.get("CODEX_FAST_REASONING_EFFORT", "low").strip()

        # Act mode configuration
        act_instructions = os.environ.get("CODEX_ACT_INSTRUCTIONS", "").strip()
        additional_prompt = os.environ.get("CODEX_ADDITIONAL_PROMPT", "").strip()

        # Validate model authentication
        if model_provider == "openai":
            has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
            has_codex_key = bool(os.environ.get("CODEX_API_KEY"))
            if not has_openai_key and not has_codex_key:
                raise ConfigurationError(
                    "Missing OPENAI_API_KEY or CODEX_API_KEY for model provider 'openai'"
                )

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
            mode=mode,
            model_provider=model_provider,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            fast_model_name=fast_model_name,
            fast_reasoning_effort=fast_reasoning_effort,
            act_instructions=act_instructions,
            debug_level=debug_level,
            stream_output=stream_output,
            dry_run=dry_run,
            additional_prompt=additional_prompt,
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

        unknown = sorted(
            key
            for key, value in kwargs.items()
            if value is not None and key not in _CONFIG_OVERRIDE_KEYS
        )
        if unknown:
            joined = ", ".join(unknown)
            raise ConfigurationError(f"Unknown configuration arguments: {joined}")

        values = asdict(config)
        for key in _CONFIG_OVERRIDE_KEYS:
            value = kwargs.get(key)
            if value is not None:
                values[key] = value

        repo_root = values.get("repo_root")
        if isinstance(repo_root, str):
            values["repo_root"] = Path(repo_root).resolve()

        return cls(**values)

    def validate(self) -> None:
        """Validate the configuration."""
        if not self.github_token:
            raise ConfigurationError("GitHub token is required")

        if not self.repository or "/" not in self.repository:
            raise ConfigurationError("Repository must be in format 'owner/repo'")

        if self.pr_number is not None and self.pr_number <= 0:
            raise ConfigurationError("PR number must be positive")

        if self.mode not in ("review", "act"):
            raise ConfigurationError(f"Invalid mode: {self.mode}. Must be 'review' or 'act'")

        if self.debug_level < 0:
            raise ConfigurationError("Debug level must be non-negative")

        if self.model_provider == "openai":
            has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
            has_codex_key = bool(os.environ.get("CODEX_API_KEY"))
            if not has_openai_key and not has_codex_key:
                raise ConfigurationError(
                    "Missing OPENAI_API_KEY or CODEX_API_KEY for model provider 'openai'"
                )

    @staticmethod
    def extract_pr_number_from_event(event: Mapping[str, Any]) -> int | None:
        """Extract PR number from a GitHub event payload."""
        pull_request = event.get("pull_request")
        if isinstance(pull_request, Mapping):
            number = pull_request.get("number")
            if isinstance(number, (int, float, str, bytes, bytearray)):
                try:
                    parsed = int(number)
                    return parsed if parsed > 0 else None
                except (TypeError, ValueError):
                    return None
            return None

        issue = event.get("issue")
        if isinstance(issue, Mapping) and issue.get("pull_request"):
            number = issue.get("number")
            if isinstance(number, (int, float, str, bytes, bytearray)):
                try:
                    parsed = int(number)
                    return parsed if parsed > 0 else None
                except (TypeError, ValueError):
                    return None
            return None
        return None

    @property
    def owner(self) -> str:
        """Extract owner from repository string."""
        return self.repository.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        """Extract repository name from repository string."""
        return self.repository.split("/", 1)[1]


def make_debug(config: ReviewConfig) -> Callable[[int, str], None]:
    """Create a debug logging callback bound to a config's debug_level."""
    level = config.debug_level

    def _debug(min_level: int, message: str) -> None:
        if level >= min_level:
            print(f"[debug{min_level}] {message}", file=sys.stderr)

    return _debug


def _parse_debug_level(value: str) -> int:
    """Parse debug level from string with fallback."""
    try:
        return int(value.strip() or "0")
    except (ValueError, AttributeError):
        return 0
