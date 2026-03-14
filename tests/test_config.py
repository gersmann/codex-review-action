from __future__ import annotations

import pytest

from cli.core.config import ReviewConfig
from cli.core.exceptions import ConfigurationError


def test_from_args_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigurationError, match="Unknown configuration arguments"):
        ReviewConfig.from_args(github_token="t", repository="o/r", unknown_flag="x")


def test_from_args_overrides_known_values_without_environment() -> None:
    config = ReviewConfig.from_args(
        github_token="token",
        repository="owner/repo",
        pr_number=1,
        openai_api_key="test-key",
        mode="review",
        debug_level=2,
    )

    assert config.github_token == "token"
    assert config.repository == "owner/repo"
    assert config.mode == "review"
    assert config.debug_level == 2


def test_from_args_merges_cli_values_with_partial_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    config = ReviewConfig.from_args(
        repository="owner/repo",
        pr_number=19,
        mode="review",
        debug_level=1,
    )

    assert config.github_token == "token"
    assert config.repository == "owner/repo"
    assert config.pr_number == 19
    assert config.debug_level == 1


def test_from_args_ignores_invalid_environment_pr_number_when_arg_is_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_NUMBER", "abc")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    config = ReviewConfig.from_args(
        github_token="token",
        repository="owner/repo",
        pr_number=17,
    )

    assert config.pr_number == 17


def test_extract_pr_number_from_pull_request_event() -> None:
    event = {"pull_request": {"number": 42}}

    assert ReviewConfig.extract_pr_number_from_event(event) == 42


def test_extract_pr_number_from_issue_comment_pr_event() -> None:
    event = {"issue": {"number": 17, "pull_request": {"url": "https://example.test"}}}

    assert ReviewConfig.extract_pr_number_from_event(event) == 17


def test_extract_pr_number_from_invalid_event_returns_none() -> None:
    event = {"issue": {"number": "nope", "pull_request": {"url": "https://example.test"}}}

    assert ReviewConfig.extract_pr_number_from_event(event) is None


def test_from_github_event_uses_environment_and_event_pr_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    config = ReviewConfig.from_github_event({"pull_request": {"number": 42}})

    assert config.repository == "owner/repo"
    assert config.pr_number == 42


def test_from_github_event_ignores_invalid_environment_pr_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_NUMBER", "abc")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    config = ReviewConfig.from_github_event({"pull_request": {"number": 42}})

    assert config.pr_number == 42


def test_from_args_requires_openai_api_key_when_provider_is_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="Missing OPENAI_API_KEY"):
        ReviewConfig.from_args(github_token="token", repository="owner/repo", pr_number=1)


def test_from_environment_parses_allowed_commenter_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_ALLOWED_COMMENTER_ASSOCIATIONS", "owner, collaborator")
    monkeypatch.setenv("PR_NUMBER", "1")

    config = ReviewConfig.from_environment()

    assert config.allowed_commenter_associations == ("OWNER", "COLLABORATOR")


def test_from_args_rejects_invalid_allowed_commenter_associations() -> None:
    with pytest.raises(ConfigurationError, match="Invalid CODEX_ALLOWED_COMMENTER_ASSOCIATIONS"):
        ReviewConfig.from_args(
            github_token="token",
            repository="owner/repo",
            pr_number=1,
            openai_api_key="test-key",
            allowed_commenter_associations=("OWNER", "NOT_A_ROLE"),
        )


def test_is_commenter_allowed_checks_normalized_association() -> None:
    config = ReviewConfig(
        github_token="token",
        repository="owner/repo",
        allowed_commenter_associations=("OWNER", "COLLABORATOR"),
    )

    assert config.is_commenter_allowed("collaborator") is True
    assert config.is_commenter_allowed("member") is False


def test_validate_requires_pr_number_in_review_mode() -> None:
    config = ReviewConfig(github_token="token", repository="owner/repo", mode="review")

    with pytest.raises(ConfigurationError, match="PR number is required in review mode"):
        config.validate()


def test_resolved_repo_root_and_context_dir_defaults(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = ReviewConfig(github_token="token", repository="owner/repo", context_dir_name="")

    assert config.resolved_repo_root == tmp_path.resolve()
    assert config.resolved_context_dir_name == ".codex-context"
