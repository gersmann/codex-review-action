from __future__ import annotations

import json
import sys

import pytest

from cli import main as main_module
from cli.core.exceptions import CodexReviewError
from cli.core.models import AckComment, ReviewRunResult
from cli.review.posting import ReviewPostingOutcome
from cli.workflows.review_workflow import (
    ReviewSummary,
    ReviewWorkflowResult,
)


def test_parser_is_review_only_with_luna_default_and_unset_reasoning() -> None:
    parser = main_module.create_parser()
    destinations = {action.dest for action in parser._actions}

    assert "mode" not in destinations
    assert "act_instructions" not in destinations

    args = parser.parse_args(["--repo", "owner/repo", "--pr", "17"])
    assert args.model_name == "gpt-5.6-luna"
    assert args.reasoning_effort is None


def test_parser_rejects_unpriced_review_model() -> None:
    with pytest.raises(SystemExit):
        main_module.create_parser().parse_args(["--model", "gpt-5.4"])


def _make_review_result(
    *, findings_count: int, carried_forward_count: int = 0
) -> ReviewWorkflowResult:
    findings: list[dict[str, object]] = []
    for index in range(findings_count):
        findings.append(
            {
                "title": f"finding-{index}",
                "body": "details",
                "confidence_score": None,
                "priority": None,
                "code_location": {
                    "absolute_file_path": f"/tmp/file-{index}.py",
                    "line_range": {"start": 1, "end": 1},
                },
            }
        )
    return ReviewWorkflowResult(
        review=ReviewRunResult.from_payload(
            {
                "overall_correctness": "patch is correct",
                "overall_explanation": "",
                "overall_confidence_score": None,
                "carried_forward": [
                    {
                        "comment_id": f"comment-{index}",
                        "current_evidence": f"evidence-{index}",
                    }
                    for index in range(carried_forward_count)
                ],
                "findings": findings,
            }
        ),
        posting_outcome=ReviewPostingOutcome.empty(findings_count),
        summary=ReviewSummary(
            overall_correctness=(
                "patch is incorrect"
                if findings_count or carried_forward_count
                else "patch is correct"
            ),
            current_findings_count=findings_count,
            carried_forward_count=carried_forward_count,
            active_findings_count=findings_count + carried_forward_count,
        ),
    )


def test_main_noops_for_non_command_comment_event(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "looks good to me",
            "user": {"login": "octocat"},
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflow must not be instantiated for non-command comment")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0


def test_main_noops_for_legacy_codex_comment(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/codex review",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflow must not be instantiated for a legacy command")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0


def test_main_noops_for_unauthorized_codex_review_comment(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review reasoning:xhigh",
            "user": {"login": "octocat"},
            "author_association": "CONTRIBUTOR",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflow must not be instantiated for an unauthorized command")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "unauthorized commenter association CONTRIBUTOR" in capsys.readouterr().out


def test_main_rejects_invalid_review_reasoning_override(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review reasoning:extreme",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflows must not run for an invalid override")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 1
    assert "Invalid reasoning effort 'extreme'" in capsys.readouterr().err


def test_main_rejects_unpriced_model_before_acknowledging(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review model:gpt-5.4",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedClient:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("invalid requests must not be acknowledged")

    monkeypatch.setattr(main_module, "GitHubClient", _UnexpectedClient)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    assert main_module.main() == 1
    assert "Unsupported review model: gpt-5.4" in capsys.readouterr().err


def test_main_runs_review_workflow_with_comment_overrides(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review reasoning:X_HIGH model:gpt-5.6-sol",
            "user": {"login": "octocat"},
            "author_association": "COLLABORATOR",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    events: list[str] = []

    class _GitHubClient:
        def __init__(self, config):
            assert config.pr_number == 17

        def acknowledge_review_request(self, pr_number, request):
            assert pr_number == 17
            assert request.comment_id == 123
            assert request.commenter_login == "octocat"
            assert request.event_name == "issue_comment"
            events.append("acknowledged")
            return AckComment(comment_id=555, event_name=request.event_name)

        def delete_ack_comment(self, pr_number, ack):
            assert pr_number == 17
            assert ack == AckComment(comment_id=555, event_name="issue_comment")
            events.append("deleted")

    class _Workflow:
        def __init__(self, config):
            assert events == ["acknowledged"]
            assert config.allowed_commenter_associations == ("MEMBER", "OWNER", "COLLABORATOR")
            assert config.pr_number == 17
            assert config.reasoning_effort == "xhigh"
            assert config.model_name == "gpt-5.6-sol"
            assert config.force_fresh_review is True

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=1)

    monkeypatch.setattr(main_module, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert events == ["acknowledged", "deleted"]
    assert "Review completed: patch is incorrect, 1 findings" in capsys.readouterr().out


def test_main_leaves_ack_comment_when_review_fails(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    events: list[str] = []

    class _GitHubClient:
        def __init__(self, config):  # noqa: ARG002
            pass

        def acknowledge_review_request(self, pr_number, request):
            events.append("acknowledged")
            return AckComment(comment_id=555, event_name=request.event_name)

        def delete_ack_comment(self, pr_number, ack):  # noqa: ARG002
            events.append("deleted")

    class _Workflow:
        def __init__(self, config):  # noqa: ARG002
            pass

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:  # noqa: ARG002
            raise CodexReviewError("boom")

    monkeypatch.setattr(main_module, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 1
    assert events == ["acknowledged"]
    assert "Review error: boom" in capsys.readouterr().err


def test_main_reports_ack_deletion_failure_without_failing_run(
    monkeypatch, tmp_path, capsys
) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _GitHubClient:
        def __init__(self, config):  # noqa: ARG002
            pass

        def acknowledge_review_request(self, pr_number, request):  # noqa: ARG002
            return AckComment(comment_id=555, event_name=request.event_name)

        def delete_ack_comment(self, pr_number, ack):  # noqa: ARG002
            raise RuntimeError("permission denied")

    class _Workflow:
        def __init__(self, config):  # noqa: ARG002
            pass

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:  # noqa: ARG002
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert (
        "Failed to delete acknowledgement comment id=555: permission denied"
        in capsys.readouterr().err
    )


def test_main_dry_run_skips_ack_and_deletion(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/review",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("DRY_RUN", "1")

    class _UnexpectedClient:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("dry runs must not touch the acknowledgement client")

    class _Workflow:
        def __init__(self, config):  # noqa: ARG002
            pass

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:  # noqa: ARG002
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "GitHubClient", _UnexpectedClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    assert main_module.main() == 0


def test_main_noops_for_unwired_verify_command_as_review_only(
    monkeypatch, tmp_path, capsys
) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/verify is the retry loop wrong?",
            "user": {"login": "octocat"},
            "author_association": "COLLABORATOR",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("review-only comment handling must not run a workflow")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "review-only" in capsys.readouterr().out


def test_main_runs_review_workflow_with_comment_defaults(monkeypatch, tmp_path) -> None:
    event_payload = {
        "pull_request": {"number": 19},
        "comment": {
            "id": 456,
            "body": "/review",
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review_comment")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)

    events: list[str] = []

    class _GitHubClient:
        def __init__(self, config):
            assert config.pr_number == 19

        def acknowledge_review_request(self, pr_number, request):
            assert pr_number == 19
            assert request.comment_id == 456
            assert request.commenter_login == "octocat"
            assert request.event_name == "pull_request_review_comment"
            events.append("acknowledged")
            return AckComment(comment_id=777, event_name=request.event_name)

        def delete_ack_comment(self, pr_number, ack):
            assert pr_number == 19
            assert ack == AckComment(comment_id=777, event_name="pull_request_review_comment")
            events.append("deleted")

    class _Workflow:
        def __init__(self, config):
            assert events == ["acknowledged"]
            assert config.pr_number == 19
            assert config.model_name == "gpt-5.6-luna"
            assert config.reasoning_effort == "xhigh"
            assert config.force_fresh_review is False

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 19
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    assert main_module.main() == 0
    assert events == ["acknowledged", "deleted"]


@pytest.mark.parametrize(
    ("body", "expected_model", "expected_effort"),
    [
        ("/review reasoning:high", "gpt-5.6-luna", "high"),
        ("/review model:gpt-5.6-terra", "gpt-5.6-terra", "medium"),
    ],
)
def test_main_comment_reasoning_defaults_follow_effective_model(
    monkeypatch, tmp_path, body: str, expected_model: str, expected_effort: str
) -> None:
    event_payload = {
        "pull_request": {"number": 19},
        "comment": {
            "id": 456,
            "body": body,
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review_comment")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)

    class _GitHubClient:
        def __init__(self, config):  # noqa: ARG002
            pass

        def acknowledge_review_request(self, pr_number, request):
            return AckComment(comment_id=777, event_name=request.event_name)

        def delete_ack_comment(self, pr_number, ack):  # noqa: ARG002
            pass

    class _Workflow:
        def __init__(self, config):
            assert config.model_name == expected_model
            assert config.reasoning_effort == expected_effort

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 19
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    assert main_module.main() == 0


def test_main_runs_review_workflow_for_actions_pr_event(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "pull_request": {"number": 17},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=1)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "Review completed: patch is incorrect, 1 findings" in capsys.readouterr().out


def test_main_runs_review_workflow(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=2)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17"],
    )

    rc = main_module.main()

    assert rc == 0
    assert "Review completed: patch is incorrect, 2 findings" in capsys.readouterr().out


def test_main_runs_review_workflow_with_cli_repo_and_env_token_only(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.github_token == "token"
            assert config.repository == "owner/repo"
            assert config.pr_number == 19

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 19
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "19"],
    )

    rc = main_module.main()

    assert rc == 0
    assert "Review completed: patch is correct, 0 findings" in capsys.readouterr().out


def test_main_reports_carried_forward_findings_separately(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=1, carried_forward_count=2)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17"],
    )

    rc = main_module.main()

    assert rc == 0
    assert (
        "Review completed: patch is incorrect, 1 new findings, "
        "2 prior findings still relevant (3 active total)"
    ) in capsys.readouterr().out


def test_main_reports_clean_summary_without_resolution_counts(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=0)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17"],
    )

    rc = main_module.main()

    assert rc == 0
    assert "Review completed: patch is correct, 0 findings" in capsys.readouterr().out


def test_main_returns_one_for_review_workflow_errors(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):  # noqa: ARG002
            pass

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            raise CodexReviewError("boom")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17"],
    )

    rc = main_module.main()

    assert rc == 1
    assert "Review error: boom" in capsys.readouterr().err


def test_parse_codex_command_verify_with_options_and_claim() -> None:
    from cli.main import _parse_codex_command

    parsed = _parse_codex_command(
        "verify model:gpt-5.6-terra reasoning:xhigh is the retry loop off by one?"
    )

    assert parsed is not None
    assert parsed.action == "verify"
    assert parsed.model_name == "gpt-5.6-terra"
    assert parsed.reasoning_effort == "xhigh"
    assert parsed.claim == "is the retry loop off by one?"


def test_parse_codex_command_verify_bare() -> None:
    from cli.main import _parse_codex_command

    parsed = _parse_codex_command("verify")

    assert parsed is not None
    assert parsed.action == "verify"
    assert parsed.model_name is None
    assert parsed.reasoning_effort is None
    assert parsed.claim == ""


def test_parse_codex_command_review_still_rejects_free_text() -> None:
    from cli.core.exceptions import ConfigurationError
    from cli.main import _parse_codex_command

    with pytest.raises(ConfigurationError):
        _parse_codex_command("review please check the loop")


def test_parse_codex_command_unknown_action_returns_none() -> None:
    from cli.main import _parse_codex_command

    assert _parse_codex_command("edit something") is None


def test_parse_codex_command_verify_options_after_claim_are_claim_text() -> None:
    from cli.main import _parse_codex_command

    parsed = _parse_codex_command("verify is the retry loop using model:gpt-4 broken?")

    assert parsed is not None
    assert parsed.model_name is None
    assert parsed.claim == "is the retry loop using model:gpt-4 broken?"
    # An invalid reasoning value after claim start must not raise either:
    parsed2 = _parse_codex_command("verify is reasoning:bananas ok")
    assert parsed2 is not None
    assert parsed2.reasoning_effort is None
    assert parsed2.claim == "is reasoning:bananas ok"
