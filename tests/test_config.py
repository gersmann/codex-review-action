from __future__ import annotations

import pytest

from cli.config import ReviewConfig
from cli.exceptions import ConfigurationError


def test_from_args_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigurationError, match="Unknown configuration arguments"):
        ReviewConfig.from_args(github_token="t", repository="o/r", unknown_flag="x")


def test_from_args_overrides_known_values_without_environment() -> None:
    config = ReviewConfig.from_args(
        github_token="token",
        repository="owner/repo",
        mode="review",
        debug_level=2,
    )

    assert config.github_token == "token"
    assert config.repository == "owner/repo"
    assert config.mode == "review"
    assert config.debug_level == 2


def test_extract_pr_number_from_pull_request_event() -> None:
    event = {"pull_request": {"number": 42}}

    assert ReviewConfig.extract_pr_number_from_event(event) == 42


def test_extract_pr_number_from_issue_comment_pr_event() -> None:
    event = {"issue": {"number": 17, "pull_request": {"url": "https://example.test"}}}

    assert ReviewConfig.extract_pr_number_from_event(event) == 17


def test_extract_pr_number_from_invalid_event_returns_none() -> None:
    event = {"issue": {"number": "nope", "pull_request": {"url": "https://example.test"}}}

    assert ReviewConfig.extract_pr_number_from_event(event) is None
