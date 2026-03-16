from __future__ import annotations

import json
import sys

from cli import main as main_module
from cli.core.exceptions import CodexReviewError
from cli.core.models import CommentContext, ReviewRunResult
from cli.review.posting import ReviewPostingOutcome
from cli.workflows.review_workflow import ReviewSummary, ReviewWorkflowResult


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
                "carried_forward_comment_ids": [
                    f"comment-{index}" for index in range(carried_forward_count)
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


def test_main_noops_for_non_codex_comment_event(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(main_module, "EditWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0


def test_main_noops_for_bare_codex_comment(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/codex",
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
            raise AssertionError("workflow must not be instantiated for a bare command")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(main_module, "EditWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0


def test_main_noops_for_unauthorized_codex_comment(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/codex fix docs",
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
    monkeypatch.setenv("CODEX_MODE", "act")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflow must not be instantiated for an unauthorized command")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(main_module, "EditWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "unauthorized commenter association CONTRIBUTOR" in capsys.readouterr().out


def test_main_fails_for_invalid_comment_payload(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": "not-an-int",
            "body": "/codex fix docs",
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
    monkeypatch.setenv("CODEX_MODE", "act")
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(main_module, "ReviewWorkflow", object)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 1
    assert "Invalid comment event payload: missing id or event name" in capsys.readouterr().err


def test_main_runs_edit_workflow_for_authorized_codex_comment(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/codex: fix docs",
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
    monkeypatch.setenv("CODEX_MODE", "act")

    calls: list[tuple[str, int, object]] = []

    class _Workflow:
        def __init__(self, config):
            assert config.allowed_commenter_associations == ("MEMBER", "OWNER", "COLLABORATOR")

        def process_edit_command(self, cmd: str, pr_number: int, comment_ctx: object) -> int:
            calls.append((cmd, pr_number, comment_ctx))
            return 7

    monkeypatch.setattr(main_module, "EditWorkflow", _Workflow)
    monkeypatch.setattr(main_module, "ReviewWorkflow", object)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 7
    assert calls == [
        (
            "fix docs",
            17,
            CommentContext(
                id=123,
                event_name="issue_comment",
                author="octocat",
                body="/codex: fix docs",
            ),
        )
    ]


def test_main_noops_for_codex_comment_in_review_mode(monkeypatch, tmp_path, capsys) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "/codex fix docs",
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
    monkeypatch.setenv("CODEX_MODE", "review")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflows must not run for /codex comments in review mode")

    monkeypatch.setattr(main_module, "EditWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "set CODEX_MODE=act to enable comment-triggered edits" in capsys.readouterr().out


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
            assert config.mode == "review"
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=1)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
    assert "Review completed: patch is incorrect, 1 findings" in capsys.readouterr().out


def test_main_runs_review_workflow_in_review_mode(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _Workflow:
        def __init__(self, config):
            assert config.mode == "review"
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=2)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17", "--mode", "review"],
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
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "19", "--mode", "review"],
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
            assert config.mode == "review"
            assert config.pr_number == 17

        def process_review(self, pr_number: int) -> ReviewWorkflowResult:
            assert pr_number == 17
            return _make_review_result(findings_count=1, carried_forward_count=2)

    monkeypatch.setattr(main_module, "ReviewWorkflow", _Workflow)
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17", "--mode", "review"],
    )

    rc = main_module.main()

    assert rc == 0
    assert (
        "Review completed: patch is incorrect, 1 new findings, "
        "2 prior findings still relevant (3 active total)"
    ) in capsys.readouterr().out


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
    monkeypatch.setattr(main_module, "EditWorkflow", object)
    monkeypatch.setattr(
        sys,
        "argv",
        ["codex-review", "--repo", "owner/repo", "--pr", "17", "--mode", "review"],
    )

    rc = main_module.main()

    assert rc == 1
    assert "Review error: boom" in capsys.readouterr().err
